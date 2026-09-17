"""``newsscore`` command line.

    newsscore source add finnhub --api-key KEY
    newsscore source add yahoo
    newsscore source list
    newsscore score AAPL --days 7
    newsscore score AAPL -s finnhub -s yahoo --json

Saved sources live in a per-user JSON file (``newsscore config-path`` shows where).
API keys may be placed in a ``.env`` file in the working directory; it is loaded
on every invocation without overriding real environment variables.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
from typing import Annotated, Optional

import typer

from ._version import __version__
from .cache import ScoreCache
from .config import SourceSpec, SourceStore, config_path, env_candidates, load_env
from .scorer import NewsScorer
from .scoring import SCORERS
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
) -> None:
    """List recent articles without scoring them."""
    scorer = NewsScorer.from_config(score_fn="keyword", cache=False)
    articles = _run(scorer, scorer.afetch(query, days=days, sources=source))
    if as_json:
        typer.echo(json.dumps([a.to_dict() for a in articles], indent=2))
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
) -> None:
    """Fetch, score and aggregate news sentiment for QUERY."""
    if scorer_name and scorer_name not in SCORERS:
        _fail(f"unknown scorer {scorer_name!r}. Known: {', '.join(sorted(SCORERS))}")
    scorer = NewsScorer.from_config(score_fn=scorer_name, cache=not no_cache, half_life_hours=half_life)
    result = _run(scorer, scorer.ascore(query, days=days, sources=source))

    if as_json:
        typer.echo(json.dumps(result.to_dict(include_articles=articles > 0 or True), indent=2, default=str))
        return

    typer.echo(f"query       {result.query}")
    typer.echo(f"window      {result.since:%Y-%m-%d} .. {result.until:%Y-%m-%d}  ({days:g} days)")
    typer.echo(f"scorer      {scorer.scorer_name or type(scorer.score_fn).__name__}")
    typer.echo(f"articles    {result.n_articles}")
    typer.echo(f"score       {result.score:+.3f}   (-1 bearish .. +1 bullish)")
    typer.echo(f"confidence  {result.confidence:.3f}")
    if result.by_source:
        typer.echo("by source   " + "  ".join(f"{k}={v:+.2f}" for k, v in sorted(result.by_source.items())))
    for message in result.errors:
        typer.secho(f"warning     {message}", fg="yellow", err=True)
    if articles:
        typer.echo("")
        for item in result.articles[:articles]:
            a, s = item.article, item.score
            typer.echo(f"{s.score:+.2f} c={s.confidence:.2f} r={s.relevance:.2f}  {a.published:%m-%d %H:%M} [{a.source}] {a.title}")


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
