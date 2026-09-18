"""``newsscore`` command line.

    newsscore source add finnhub --api-key KEY
    newsscore source add yahoo
    newsscore source list
    newsscore score AAPL --days 7
    newsscore score AAPL -s finnhub -s yahoo --json
    newsscore fetch AAPL --out news.json

Nothing is written unless you ask for it: ``fetch`` and ``score`` print to stdout,
and ``--out FILE`` saves the same payload as JSON instead. Article *scores* are
cached in SQLite so a repeated query only pays for new articles, and saved sources
live in a per-user JSON file. ``newsscore doctor`` prints every path in use; the
``NEWSSCORE_CACHE``, ``NEWSSCORE_CONFIG`` and ``NEWSSCORE_ENV`` variables move them.

API keys may be placed in a ``.env`` file in the working directory; it is loaded
on every invocation without overriding real environment variables.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import sys
from pathlib import Path
from typing import Annotated, Optional

import typer

from ._version import __version__
from .cache import ScoreCache, default_cache_path
from .config import SourceSpec, SourceStore, config_path, env_candidates, load_env
from .models import RunStatus, parse_dt
from .scorer import NewsScorer
from .scoring import SCORERS, ScorerUnavailable
from .sources import SOURCE_TYPES, SourceError, make_source

app = typer.Typer(
    help="News sentiment scoring for stocks. Mainstream news APIs in, one score out.",
    invoke_without_command=True,
    add_completion=False,
)
source_app = typer.Typer(help="Manage saved news sources.", no_args_is_help=True)
app.add_typer(source_app, name="source")

SourcesOpt = Annotated[
    Optional[list[str]],
    typer.Option("--source", "-s", help="Saved source name; repeat for several. Omit for all."),
]
DaysOpt = Annotated[float, typer.Option("--days", "-d", help="Look-back window in days.")]
JsonOpt = Annotated[bool, typer.Option("--json", help="Machine-readable output.")]
OutOpt = Annotated[
    Optional[Path],
    typer.Option("--out", metavar="FILE", help="Write the result to FILE as JSON instead of printing it."),
]


EXIT_UNUSABLE = 3  # ran, but produced no aggregate worth reading
EXIT_PARTIAL = 4  # produced an aggregate, but some articles failed to score

FailOnOpt = Annotated[
    str,
    typer.Option(
        "--fail-on",
        metavar="LEVEL",
        help=(
            "Strict exit policy. 'none' (default) always exits 0; 'unusable' exits "
            f"{EXIT_UNUSABLE} when there is no usable aggregate (no news, all scoring failed, "
            f"or no weight); 'partial' additionally exits {EXIT_PARTIAL} when any article failed. "
            "Diagnostics and partial results are still written either way."
        ),
    ),
]
FAIL_ON_LEVELS = ("none", "unusable", "partial")


def _exit_code(status: str, fail_on: str) -> int:
    """The documented strict policy. Returns 0 when the run satisfies `fail_on`."""
    if fail_on == "none":
        return 0
    if status in RunStatus.FAILED:
        return EXIT_UNUSABLE
    if fail_on == "partial" and status != RunStatus.OK:
        return EXIT_PARTIAL
    return 0


@app.callback()
def _main(
    ctx: typer.Context,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show warnings from sources.")] = False,
    version: Annotated[bool, typer.Option("--version", help="Print version and exit.", is_eager=True)] = False,
) -> None:
    if version:
        typer.echo(__version__)
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit()
    logging.basicConfig(level=logging.INFO if verbose else logging.ERROR, format="%(levelname)s %(message)s")
    load_env()


# ---- source management ------------------------------------------------------------


@source_app.command("add")
def source_add(
    type_: Annotated[str, typer.Argument(metavar="TYPE", help="Source type; see `newsscore source types`.")],
    name: Annotated[Optional[str], typer.Option("--name", "-n", help="Name to save under (default: the type).")] = None,
    api_key: Annotated[Optional[str], typer.Option("--api-key", "-k", help="Provider API key.")] = None,
    option: Annotated[
        Optional[list[str]], typer.Option("--option", "-o", metavar="KEY=VALUE", help="Provider option; repeatable.")
    ] = None,
    replace: Annotated[bool, typer.Option("--replace", help="Overwrite a source with the same name.")] = False,
) -> None:
    """Save a news source to the local config."""
    if type_ not in SOURCE_TYPES:
        _fail(f"unknown type {type_!r}. Known: {', '.join(sorted(SOURCE_TYPES))}")
    options = _parse_options(option or [])
    spec = SourceSpec(type=type_, name=name or type_, api_key=api_key, options=options)
    try:  # construct once to surface bad options early; a missing key is only a warning
        make_source(type_, api_key=api_key, name=spec.name, **options)
    except SourceError as exc:
        if "API key required" not in str(exc):
            _fail(str(exc))
        typer.secho(f"warning: {exc}. Saved anyway; set the env var before use.", fg="yellow", err=True)
    try:
        SourceStore().add(spec, replace=replace)
    except KeyError as exc:
        _fail(f"{exc.args[0]}; pass --replace to overwrite")
    typer.echo(f"saved source {spec.name!r} ({type_}) to {config_path()}")


@source_app.command("list")
def source_list() -> None:
    """Show saved sources."""
    specs = SourceStore().load()
    if not specs:
        typer.echo("no saved sources. Try: newsscore source add yahoo")
        return
    rows = [(s.name, s.type, _mask(s.api_key), json.dumps(s.options) if s.options else "") for s in specs.values()]
    _table(("NAME", "TYPE", "API KEY", "OPTIONS"), rows)


@source_app.command("remove")
def source_remove(name: Annotated[str, typer.Argument(help="Saved source name.")]) -> None:
    """Delete a saved source."""
    try:
        SourceStore().remove(name)
    except KeyError as exc:
        _fail(exc.args[0])
    typer.echo(f"removed {name!r}")


@source_app.command("types")
def source_types() -> None:
    """List supported source types."""
    rows = [
        (t, "no" if not cls.requires_key else "yes", cls.env_key or "", cls.query_kind)
        for t, cls in sorted(SOURCE_TYPES.items())
    ]
    _table(("TYPE", "NEEDS KEY", "ENV VAR", "QUERY"), rows)


# ---- fetching and scoring ---------------------------------------------------------


@app.command()
def fetch(
    query: Annotated[str, typer.Argument(help="Ticker symbol or keyword.")],
    source: SourcesOpt = None,
    days: DaysOpt = 7,
    as_json: JsonOpt = False,
    out: OutOpt = None,
) -> None:
    """List recent articles without scoring them.

    Articles are printed and then forgotten; pass --out FILE to keep them as JSON.
    """
    scorer = NewsScorer.from_config(score_fn="keyword", cache=False)  # fetch never scores
    articles = _run(scorer, scorer.afetch(query, days=days, sources=source))
    payload = [a.to_dict() for a in articles]
    if out:
        _write_json(out, payload)
        typer.echo(f"wrote {len(articles)} article(s) to {out}")
        return
    if as_json:
        typer.echo(json.dumps(payload, indent=2))
        return
    if not articles:
        typer.echo("no articles found")
        return
    for a in articles:
        typer.echo(f"{a.published:%Y-%m-%d %H:%M}  [{a.source}]  {a.title}")
    typer.echo(f"\n{len(articles)} article(s)")


@app.command()
def score(
    query: Annotated[str, typer.Argument(help="Ticker symbol or keyword.")],
    source: SourcesOpt = None,
    days: DaysOpt = 7,
    scorer_name: Annotated[
        Optional[str], typer.Option("--scorer", help=f"One of: {', '.join(sorted(SCORERS))}. Default: jev if configured, else keyword.")
    ] = None,
    half_life: Annotated[float, typer.Option("--half-life", help="Decay half-life in hours for aggregation.")] = 48.0,
    no_cache: Annotated[bool, typer.Option("--no-cache", help="Do not read or write the score cache.")] = False,
    articles: Annotated[int, typer.Option("--articles", "-a", help="Show the N most recent scored articles.")] = 0,
    as_json: JsonOpt = False,
    out: OutOpt = None,
    fail_on: FailOnOpt = "none",
) -> None:
    """Fetch, score and aggregate news sentiment for QUERY.

    The result is printed; pass --out FILE to keep the full JSON (every scored
    article included) instead.
    """
    if scorer_name and scorer_name not in SCORERS:
        _fail(f"unknown scorer {scorer_name!r}. Known: {', '.join(sorted(SCORERS))}")
    if fail_on not in FAIL_ON_LEVELS:
        _fail(f"unknown --fail-on {fail_on!r}. Known: {', '.join(FAIL_ON_LEVELS)}")
    try:
        scorer = NewsScorer.from_config(score_fn=scorer_name, cache=not no_cache, half_life_hours=half_life)
    except ScorerUnavailable as exc:  # asked for a scorer by name that cannot run here
        _fail(str(exc))
    result = _run(scorer, scorer.ascore(query, days=days, sources=source))
    code = _exit_code(result.status, fail_on)

    _emit(result, scorer, code, window=f"  ({days:g} days)", articles=articles, as_json=as_json, out=out)


@app.command("score-articles")
def score_articles(
    file: Annotated[
        Optional[Path],
        typer.Argument(metavar="FILE", help="JSON input; omit or pass - to read stdin."),
    ] = None,
    query: Annotated[
        Optional[str], typer.Option("--query", "-q", help="Target symbol or company. Overrides the file.")
    ] = None,
    as_of: Annotated[
        Optional[str],
        typer.Option("--as-of", help="Reference time for decay and eligibility (ISO 8601). Default: now."),
    ] = None,
    no_dedupe: Annotated[bool, typer.Option("--no-dedupe", help="Keep syndicated copies.")] = False,
    scorer_name: Annotated[
        Optional[str], typer.Option("--scorer", help=f"One of: {', '.join(sorted(SCORERS))}.")
    ] = None,
    half_life: Annotated[float, typer.Option("--half-life", help="Decay half-life in hours.")] = 48.0,
    no_cache: Annotated[bool, typer.Option("--no-cache", help="Do not read or write the score cache.")] = False,
    articles: Annotated[int, typer.Option("--articles", "-a", help="Show the N most recent scored articles.")] = 0,
    as_json: JsonOpt = False,
    out: OutOpt = None,
    fail_on: FailOnOpt = "none",
) -> None:
    """Score articles you already have, without fetching any news.

    FILE is either a JSON array of articles, or an object with a "query" and an
    "articles" list (and optionally "as_of"). Each article needs "title" and
    "published"; "id", "source", "url", "summary" and "symbols" are kept as given
    and never regenerated. The output of `newsscore fetch --out` is valid input.

        {"query": "AAPL", "articles": [
          {"id": "a1", "source": "internal", "title": "Apple beats estimates",
           "published": "2026-09-17T14:00:00Z", "url": "https://...",
           "summary": "...", "symbols": ["AAPL"]}
        ]}

    Scoring one article for two targets is two separate runs, cached separately.
    """
    if fail_on not in FAIL_ON_LEVELS:
        _fail(f"unknown --fail-on {fail_on!r}. Known: {', '.join(FAIL_ON_LEVELS)}")
    if scorer_name and scorer_name not in SCORERS:
        _fail(f"unknown scorer {scorer_name!r}. Known: {', '.join(sorted(SCORERS))}")

    payload = _read_json_input(file)
    if isinstance(payload, dict):
        items = payload.get("articles")
        query = query or payload.get("query")
        as_of = as_of or payload.get("as_of") or payload.get("until")
    else:
        items = payload
    if not isinstance(items, list):
        _fail('input must be a JSON array of articles, or an object with an "articles" array')
    if not query:
        _fail("no target: pass --query, or put a \"query\" key in the input")

    try:
        scorer = NewsScorer.from_config(score_fn=scorer_name, cache=not no_cache, half_life_hours=half_life)
    except ScorerUnavailable as exc:
        _fail(str(exc))
    try:
        result = _run(
            scorer,
            scorer.ascore_articles(items, query, as_of=_parse_as_of(as_of), dedupe=not no_dedupe),
        )
    except ValueError as exc:  # a malformed article: say which one, do not guess
        _fail(str(exc))
    _emit(result, scorer, _exit_code(result.status, fail_on), articles=articles, as_json=as_json, out=out)


@app.command("config-path")
def show_config_path() -> None:
    """Print where saved sources are stored."""
    typer.echo(str(config_path()))


@app.command("cache-clear")
def cache_clear(
    scorer_name: Annotated[Optional[str], typer.Option("--scorer", help="Only clear this scorer's entries.")] = None,
) -> None:
    """Delete cached article scores."""
    with ScoreCache() as cache:
        n = cache.clear(scorer_name)
    typer.echo(f"removed {n} cached score(s) from {ScoreCache().path}")


@app.command()
def doctor() -> None:
    """Check what is configured on this machine."""
    env_file = next((p for p in env_candidates() if p.is_file()), None)
    typer.echo(f"env file      {env_file or 'none found (looked in ' + ', '.join(str(p) for p in env_candidates()) + ')'}")
    typer.echo(f"config file   {config_path()}  ({'exists' if config_path().exists() else 'missing'})")
    cache = default_cache_path()
    typer.echo(f"score cache   {cache}  ({'exists' if cache.exists() else 'created on first score'})")
    typer.echo(f"saved sources {', '.join(SourceStore().load()) or 'none'}")
    missing = []
    if importlib.util.find_spec("typesafe_sdk") is None:
        missing.append("typesafe-sdk not installed (pip install 'newsscore[jev]' or uv sync)")
    if not os.environ.get("TYPESAFE_API_KEY"):
        missing.append("TYPESAFE_API_KEY not set")
    typer.echo(f"jev scorer    {'ready' if not missing else 'unavailable: ' + '; '.join(missing)}")


# ---- helpers ---------------------------------------------------------------------------


def _run(scorer: NewsScorer, coro):  # type: ignore[no-untyped-def]
    async def go():  # type: ignore[no-untyped-def]
        try:
            return await coro
        finally:
            await scorer.aclose()

    return asyncio.run(go())


def _emit(result, scorer, code: int, *, window: str = "", articles: int = 0, as_json: bool = False, out=None) -> None:
    """Render one result. Machine-readable output goes to stdout, diagnostics to stderr."""
    if out:
        _write_json(out, result.to_dict())
        typer.echo(f"wrote {result.n_articles} scored article(s) to {out}")
        raise typer.Exit(code=code)
    if as_json:
        typer.echo(json.dumps(result.to_dict(), indent=2, default=str))
        for message in result.errors:
            typer.secho(f"warning     {message}", fg="yellow", err=True)
        raise typer.Exit(code=code)

    c = result.counts
    typer.echo(f"query       {result.query}")
    typer.echo(f"window      {result.since:%Y-%m-%d} .. {result.until:%Y-%m-%d}{window}")
    typer.echo(f"scorer      {scorer.scorer_name or type(scorer.score_fn).__name__}")
    typer.echo(f"articles    {result.n_articles}")
    typer.echo(f"status      {result.status}")
    typer.echo(
        f"counts      fetched={c.fetched} deduplicated={c.deduplicated} filtered={c.filtered} "
        f"submitted={c.submitted} scored={c.scored} failed={c.failed}"
    )
    typer.echo(f"score       {result.score:+.3f}   (-1 bearish .. +1 bullish)")
    typer.echo(f"confidence  {result.confidence:.3f}")
    if result.by_source:
        typer.echo("by source   " + "  ".join(f"{k}={v:+.2f}" for k, v in sorted(result.by_source.items())))
    for message in result.errors:
        typer.secho(f"warning     {message}", fg="yellow", err=True)
    if result.status in RunStatus.FAILED:
        typer.secho(
            f"note        score {result.score:+.3f} is not a sentiment reading here "
            f"(status={result.status}); check counts before using it.",
            fg="yellow",
            err=True,
        )
    if articles:
        typer.echo("")
        for item in result.articles[:articles]:
            a, sc = item.article, item.score
            typer.echo(
                f"{sc.score:+.2f} c={sc.confidence:.2f} r={sc.relevance:.2f}  "
                f"{a.published:%m-%d %H:%M} [{a.source}] {a.title}"
            )
    if code:
        raise typer.Exit(code=code)


def _read_json_input(file: Optional[Path]) -> object:
    """Read the articles payload from FILE, or from stdin when it is absent or '-'."""
    if file is None or str(file) == "-":
        text = sys.stdin.read()
        if not text.strip():
            _fail("no input on stdin; pass a FILE or pipe JSON in")
    else:
        try:
            text = Path(file).expanduser().read_text(encoding="utf-8")
        except OSError as exc:
            _fail(f"could not read {file}: {exc}")
    try:
        return json.loads(text)
    except ValueError as exc:
        _fail(f"input is not valid JSON: {exc}")


def _parse_as_of(value):
    if not value:
        return None
    try:
        return parse_dt(value)
    except ValueError as exc:
        _fail(f"bad --as-of: {exc}")


def _write_json(path: Path, payload: object) -> None:
    """Save `payload` as UTF-8 JSON, creating parent directories as needed."""
    path = path.expanduser()
    if path.parent != Path(""):
        path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    except OSError as exc:
        _fail(f"could not write {path}: {exc}")


def _parse_options(items: list[str]) -> dict[str, object]:
    options: dict[str, object] = {}
    for item in items:
        key, sep, value = item.partition("=")
        if not sep or not key:
            _fail(f"bad --option {item!r}; expected KEY=VALUE")
        lowered = value.lower()
        options[key] = True if lowered == "true" else False if lowered == "false" else _number_or_str(value)
    return options


def _number_or_str(value: str) -> object:
    try:
        return int(value)
    except ValueError:
        return value


def _mask(key: str | None) -> str:
    if not key:
        return "(env)"
    return key if len(key) <= 6 else f"{key[:3]}...{key[-3:]}"


def _table(header: tuple[str, ...], rows: list[tuple[str, ...]]) -> None:
    widths = [max(len(str(r[i])) for r in (header, *rows)) for i in range(len(header))]
    for row in (header, *rows):
        typer.echo("  ".join(str(cell).ljust(w) for cell, w in zip(row, widths)).rstrip())


def _fail(message: str) -> None:
    typer.secho(f"error: {message}", fg="red", err=True)
    raise typer.Exit(code=1)


if __name__ == "__main__":  # pragma: no cover
    app()
