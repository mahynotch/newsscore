"""The engine: sources in, scoring function in the middle, one result out."""

from __future__ import annotations

import asyncio
import logging
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Sequence

if TYPE_CHECKING:  # only annotations need httpx here; fetching imports it for real
    import httpx

from .aggregate import AggregateFn, make_aggregator
from .cache import ScoreCache
from .config import SourceStore, load_env
from .http import make_client
from .models import (
    Article,
    ArticleScore,
    RunCounts,
    RunStatus,
    ScoreFailure,
    ScoredArticle,
    ScoreResult,
    parse_dt,
    to_utc,
    utcnow,
)
from .scoring import (
    ScoreFn,
    ScoreItemError,
    call_score_fn,
    default_scorer,
    make_scorer,
    scorer_fingerprint,
    scorer_name,
)
from .scoring.protocol import error_slug, safe_str
from .sources import NewsSource, SourceError, make_source

log = logging.getLogger(__name__)
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


class NewsScorer:
    """Fetch news for a symbol or keyword and score it.

    Typical use::

        scorer = NewsScorer()                       # Jev if configured, else keyword scorer
        scorer.source_add("finnhub", api_key="...")
        scorer.source_add("yahoo")                  # keyless RSS
        result = scorer.score("AAPL", days=7)       # or: await scorer.ascore("AAPL")
        print(result.score, result.confidence, result.n_articles)

    Args:
        score_fn: A scoring function (see ``newsscore.scoring.protocol``), the
            name of a built-in one (``"jev"``, ``"keyword"``) or ``None`` for the default.
        scorer_name: Stable cache key for ``score_fn``; inferred when possible.
        cache: ``True`` for the default SQLite cache, ``False`` to disable, or a path.
        half_life_hours: Decay half-life used by the default aggregator.
        impact_weights: Switch on impact weighting with a mapping such as
            :data:`~newsscore.aggregate.IMPACT_WEIGHTS`. Off by default; articles
            without an ``expected_impact`` always weigh ``1.0``.
        use_relevance: Set ``False`` to drop the relevance term from aggregation.
        lookback_hours: Ignore articles older than this when aggregating. Separate
            from the fetch window, which decides what is collected in the first place.
        aggregate_fn: Replace the default aggregation entirely.
        batch_size: Articles handed to ``score_fn`` per call.
        concurrency: Max concurrent ``score_fn`` calls.
        retries: Extra attempts for *retryable* scoring failures (timeouts, rate
            limits, 5xx). Successful articles are never rescored by a retry.
        retry_backoff: Base seconds for exponential backoff between attempts,
            with jitter. ``attempt n`` waits about ``retry_backoff * 2**(n-1)``.
        timeout: HTTP timeout in seconds for news providers.
        http_client: Reuse your own ``httpx.AsyncClient`` instead of a per-call one.
    """

    def __init__(
        self,
        score_fn: ScoreFn | str | None = None,
        *,
        scorer_name: str | None = None,
        cache: bool | str | Path = True,
        half_life_hours: float = 48.0,
        impact_weights: Mapping[str, float] | None = None,
        use_relevance: bool = True,
        lookback_hours: float | None = None,
        aggregate_fn: AggregateFn | None = None,
        batch_size: int = 16,
        concurrency: int = 4,
        retries: int = 2,
        retry_backoff: float = 0.5,
        timeout: float = 20.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.score_fn: ScoreFn = (
            default_scorer() if score_fn is None else make_scorer(score_fn) if isinstance(score_fn, str) else score_fn
        )
        self.scorer_name = scorer_name or _infer_name(self.score_fn)
        self.aggregate_fn = aggregate_fn or make_aggregator(
            half_life_hours,
            impact_weights=impact_weights,
            use_relevance=use_relevance,
            lookback_hours=lookback_hours,
        )
        self.batch_size = max(1, batch_size)
        self.concurrency = max(1, concurrency)
        self.retries = max(0, retries)
        self.retry_backoff = max(0.0, retry_backoff)
        self.timeout = timeout
        self._http_client = http_client
        self._sources: dict[str, NewsSource] = {}

        self.fingerprint = scorer_fingerprint(self.score_fn)

        self.cache: ScoreCache | None = None
        if cache and self.scorer_name is None:
            log.warning("score_fn has no stable name; caching disabled (set scorer_name= to enable)")
        elif cache and self.fingerprint is None:
            # The scorer cannot promise which model answers, so a stored score could
            # not be attributed later. Computing without caching is the safe half.
            log.warning(
                "%s declines caching under its current settings (an unpinned model); "
                "scores will be recomputed every run",
                self.scorer_name,
            )
        elif cache:
            self.cache = ScoreCache(None if cache is True else cache)

    # ---- sources -----------------------------------------------------------------

    def source_add(
        self,
        source: str | NewsSource,
        /,
        *,
        api_key: str | None = None,
        name: str | None = None,
        **options: Any,
    ) -> NewsSource:
        """Register a source by type name (``"finnhub"``) or as a ready instance.

        Returns the instance so you can keep a handle on it. Names must be unique.
        """
        if isinstance(source, str):
            source = make_source(source, api_key=api_key, name=name, **options)
        elif api_key or name or options:
            raise TypeError("api_key/name/options only apply when adding a source by type name")
        if source.name in self._sources:
            raise ValueError(f"a source named {source.name!r} is already registered")
        self._sources[source.name] = source
        return source

    def source_remove(self, name: str) -> None:
        del self._sources[name]

    @property
    def sources(self) -> dict[str, NewsSource]:
        """Registered sources by name (a copy; mutate through ``source_add``/``source_remove``)."""
        return dict(self._sources)

    @classmethod
    def from_config(
        cls, path: Path | str | None = None, *, env_file: Path | str | None = None, **kwargs: Any
    ) -> "NewsScorer":
        """Build a scorer pre-loaded with the sources saved by ``newsscore source add``.

        A ``.env`` file (``env_file``, else ``$NEWSSCORE_ENV``, ``./.env``, or the
        config directory) is loaded first so API keys can live there. Sources
        that cannot be constructed (typically a missing key) are skipped with a
        warning rather than failing the whole load.
        """
        load_env(env_file)
        scorer = cls(**kwargs)
        for spec in SourceStore(path).load().values():
            try:
                scorer.source_add(spec.type, api_key=spec.api_key, name=spec.name, **spec.options)
            except SourceError as exc:
                log.warning("skipping saved source %s: %s", spec.name, exc)
        return scorer

    # ---- public API ----------------------------------------------------------------

    async def afetch(
        self,
        query: str,
        *,
        days: float = 7,
        since: datetime | None = None,
        until: datetime | None = None,
        sources: Iterable[str] | None = None,
    ) -> list[Article]:
        """Fetch and de-duplicate articles across the selected sources, newest first."""
        since, until = _window(days, since, until)
        articles, errors, _ = await self._fetch(query, since, until, sources)
        for message in errors:
            log.warning(message)
        return articles

    async def ascore(
        self,
        query: str,
        *,
        days: float = 7,
        since: datetime | None = None,
        until: datetime | None = None,
        sources: Iterable[str] | None = None,
    ) -> ScoreResult:
        """Fetch, score and aggregate. ``sources=None`` means every registered source."""
        since, until = _window(days, since, until)
        articles, errors, fetched = await self._fetch(query, since, until, sources)
        scored, score_errors, failures = await self._score(articles, query)
        errors.extend(score_errors)
        return self._result(
            query,
            since,
            until,
            fetched=fetched,
            deduplicated=fetched - len(articles),
            filtered=0,  # sources already drop anything outside the window
            scored=scored,
            failures=failures,
            errors=errors,
        )

    async def ascore_articles(
        self,
        articles: Iterable[Article | Mapping[str, Any]],
        query: str,
        *,
        as_of: datetime | None = None,
        since: datetime | None = None,
        dedupe: bool = True,
    ) -> ScoreResult:
        """Score news you already have, without fetching anything.

        For pipelines that collect their own articles and want only the scoring and
        aggregation. Scores go through the same cache, scorer and aggregator as
        :meth:`ascore`, so the two paths are comparable on identical input.

        Args:
            articles: :class:`~newsscore.Article` objects, or mappings shaped like
                :meth:`Article.to_dict` (``title`` and ``published`` required). Ids,
                titles, times, sources and urls are carried through untouched.
            query: The target symbol or company. The same article scored for two
                targets is two independent tasks, cached and reported separately.
            as_of: Reference time for decay, and the upper bound for eligibility.
                Defaults to now. Anything published after it is *filtered*, never
                counted as fresh evidence.
            since: Optional lower bound; by default every article is eligible.
            dedupe: Drop syndicated copies, as the fetching path does.

        Returns:
            A :class:`~newsscore.ScoreResult` whose ``counts`` account for every item
            handed in: ``fetched == deduplicated + filtered + submitted``.
        """
        until = to_utc(as_of) if as_of else utcnow()
        lower = to_utc(since) if since else None
        supplied = [a if isinstance(a, Article) else Article.from_dict(a) for a in articles]

        eligible = [
            a for a in supplied if a.published <= until and (lower is None or a.published >= lower)
        ]
        filtered = len(supplied) - len(eligible)
        selected = _dedupe(eligible) if dedupe else sorted(eligible, key=lambda a: a.published, reverse=True)

        scored, errors, failures = await self._score(selected, query)
        window_start = lower or min((a.published for a in selected), default=until)
        return self._result(
            query,
            window_start,
            until,
            fetched=len(supplied),
            deduplicated=len(eligible) - len(selected),
            filtered=filtered,
            scored=scored,
            failures=failures,
            errors=errors,
        )

    def aggregate_scored(
        self,
        scored: Iterable[ScoredArticle | Mapping[str, Any]],
        query: str,
        *,
        as_of: datetime | str | None = None,
    ) -> ScoreResult:
        """Re-aggregate articles that already carry scores. Never calls the scorer.

        Use it to try different aggregation settings against a saved run: build a
        scorer with the new ``half_life_hours`` (or a different ``aggregate_fn``) and
        hand the scored articles back. No model calls, no network, no cost.

            saved = json.loads(Path("aapl.json").read_text())
            NewsScorer(half_life_hours=12, cache=False).aggregate_scored(
                saved["articles"], saved["query"], as_of=saved["until"]
            )
        """
        until = (
            to_utc(as_of) if isinstance(as_of, datetime) else parse_dt(as_of) if as_of else utcnow()
        )
        supplied = [s if isinstance(s, ScoredArticle) else ScoredArticle.from_dict(s) for s in scored]
        eligible = [s for s in supplied if s.article.published <= until]
        items = sorted(eligible, key=lambda s: s.article.published, reverse=True)
        window_start = min((s.article.published for s in items), default=until)
        return self._result(
            query,
            window_start,
            until,
            fetched=len(supplied),
            deduplicated=0,
            filtered=len(supplied) - len(eligible),
            scored=items,
            failures=[],
            errors=[],
        )

    def _result(
        self,
        query: str,
        since: datetime,
        until: datetime,
        *,
        fetched: int,
        deduplicated: int,
        filtered: int,
        scored: list[ScoredArticle],
        failures: list[ScoreFailure],
        errors: list[str],
    ) -> ScoreResult:
        """Aggregate and package one run. Shared by every public entry point."""
        agg = self.aggregate_fn(scored, until)
        usage = _run_usage(scored)
        contributions = list(getattr(agg, "contributions", []) or [])
        total_weight = getattr(agg, "total_weight", None)
        counts = RunCounts(
            fetched=fetched,
            deduplicated=deduplicated,
            filtered=filtered,
            submitted=len(scored) + len(failures),
            scored=len(scored),
            failed=len(failures),
        )
        return ScoreResult(
            query=query,
            since=since,
            until=until,
            score=agg.score,
            confidence=agg.confidence,
            n_articles=len(scored),
            by_source=agg.by_source,
            articles=scored,
            errors=errors,
            status=_status(counts, scored, total_weight),
            counts=counts,
            failures=failures,
            usage=usage,
            total_weight=0.0 if total_weight is None else total_weight,
            contributions=contributions,
        )

    def fetch(self, query: str, **kwargs: Any) -> list[Article]:
        """Blocking version of :meth:`afetch`."""
        return _run(self.afetch(query, **kwargs))

    def score(self, query: str, **kwargs: Any) -> ScoreResult:
        """Blocking version of :meth:`ascore`."""
        return _run(self.ascore(query, **kwargs))

    def score_articles(
        self, articles: Iterable[Article | Mapping[str, Any]], query: str, **kwargs: Any
    ) -> ScoreResult:
        """Blocking version of :meth:`ascore_articles`."""
        return _run(self.ascore_articles(articles, query, **kwargs))

    async def aclose(self) -> None:
        if self.cache is not None:
            self.cache.close()
        closer = getattr(self.score_fn, "aclose", None)
        if closer is not None:
            await closer()

    # ---- internals -----------------------------------------------------------------

    def _select(self, names: Iterable[str] | None) -> list[NewsSource]:
        if names is None:
            return list(self._sources.values())
        wanted = list(names)
        unknown = [n for n in wanted if n not in self._sources]
        if unknown:
            raise KeyError(f"unknown source(s): {', '.join(unknown)}; registered: {', '.join(self._sources) or 'none'}")
        return [self._sources[n] for n in wanted]

    async def _fetch(
        self, query: str, since: datetime, until: datetime, names: Iterable[str] | None
    ) -> tuple[list[Article], list[str], int]:
        """Returns (de-duplicated articles, errors, how many were fetched before de-duplication)."""
        selected = self._select(names)
        if not selected:
            return [], ["no sources registered; add one with source_add() or `newsscore source add`"], 0

        async def one(source: NewsSource, client: httpx.AsyncClient) -> list[Article]:
            return await source.fetch(query, since, until, client)

        if self._http_client is not None:
            results = await asyncio.gather(*(one(s, self._http_client) for s in selected), return_exceptions=True)
        else:
            async with make_client(self.timeout) as client:
                results = await asyncio.gather(*(one(s, client) for s in selected), return_exceptions=True)

        articles: list[Article] = []
        errors: list[str] = []
        for source, result in zip(selected, results):
            if isinstance(result, BaseException):
                errors.append(str(result) if isinstance(result, SourceError) else f"{source.name}: {result!r}")
            else:
                articles.extend(result)
        return _dedupe(articles), errors, len(articles)

    async def _score(
        self, articles: Sequence[Article], query: str
    ) -> tuple[list[ScoredArticle], list[str], list[ScoreFailure]]:
        """Score what is not cached, keeping every success even when siblings fail."""
        if not articles:
            return [], [], []
        by_id = {a.id: a for a in articles}
        scores: dict[str, ArticleScore] = {}
        if self.cache is not None and self.scorer_name and self.fingerprint is not None:
            cached = self.cache.get_many(self.scorer_name, self.fingerprint, query, articles)
            scores.update({aid: _mark_cache_hit(score, True) for aid, score in cached.items()})
        pending = [a for a in articles if a.id not in scores]
        if not pending:
            return [ScoredArticle(a, scores[a.id]) for a in articles if a.id in scores], [], []

        state = _RunState(semaphore=asyncio.Semaphore(self.concurrency))
        batches = [pending[i : i + self.batch_size] for i in range(0, len(pending), self.batch_size)]
        outcomes = await asyncio.gather(*(self._score_batch(b, query, state) for b in batches))

        fresh: dict[str, ArticleScore] = {}
        failures: dict[str, ScoreFailure] = {}
        for ok, bad in outcomes:
            fresh.update(ok)
            failures.update(bad)

        # Cache the successes even though siblings failed, so a rerun never pays twice.
        if fresh and self.cache is not None and self.scorer_name and self.fingerprint is not None:
            self.cache.put_many(self.scorer_name, self.fingerprint, query, by_id, fresh)
        scores.update({aid: _mark_cache_hit(score, False) for aid, score in fresh.items()})

        errors = [f"scoring stopped: {state.fatal}"] if state.fatal is not None else []
        errors.extend(_summarise(failures.values()))
        scored = [ScoredArticle(a, scores[a.id]) for a in articles if a.id in scores]
        return scored, errors, sorted(failures.values(), key=lambda f: f.article_id)

    async def _score_batch(
        self, batch: Sequence[Article], query: str, state: "_RunState", attempt: int = 1
    ) -> tuple[dict[str, ArticleScore], dict[str, ScoreFailure]]:
        """One attempt at one batch. Never raises; returns (scores, failures) by article id.

        A scorer that reports per-article errors (the preferred form) loses only the
        articles it named. A scorer that raises loses nothing either: a retryable
        error is retried, and anything else makes the batch split in half until the
        offending article is alone, so its siblings still get scored.
        """
        if state.fatal is not None:  # a sibling already hit something run-wide
            return {}, {a.id: _failure(a, state.fatal, attempt) for a in batch}
        try:
            async with state.semaphore:
                results = await call_score_fn(self.score_fn, batch, query)
        except Exception as exc:
            return await self._recover(batch, query, state, attempt, _classify(exc))

        ok: dict[str, ArticleScore] = {}
        bad: dict[str, ScoreFailure] = {}
        retry: list[Article] = []
        for article, result in zip(batch, results):
            if not isinstance(result, ScoreItemError):
                ok[article.id] = result
            elif result.fatal:
                state.fatal = result
                bad[article.id] = _failure(article, result, attempt)
            elif result.retryable and attempt <= self.retries:
                retry.append(article)
            else:
                bad[article.id] = _failure(article, result, attempt)

        if retry:
            await asyncio.sleep(_backoff(self.retry_backoff, attempt))
            more_ok, more_bad = await self._score_batch(retry, query, state, attempt + 1)
            ok.update(more_ok)
            bad.update(more_bad)
        return ok, bad

    async def _recover(
        self,
        batch: Sequence[Article],
        query: str,
        state: "_RunState",
        attempt: int,
        error: ScoreItemError,
    ) -> tuple[dict[str, ArticleScore], dict[str, ScoreFailure]]:
        """The call itself raised, so every article in the batch is suspect."""
        if error.fatal:  # bad credentials, missing dependency: stop, do not repeat it n times
            state.fatal = error
            return {}, {a.id: _failure(a, error, attempt) for a in batch}
        if error.retryable and attempt <= self.retries:
            await asyncio.sleep(_backoff(self.retry_backoff, attempt))
            return await self._score_batch(batch, query, state, attempt + 1)
        if len(batch) > 1:  # bisect: find the bad article instead of discarding the batch
            half = len(batch) // 2
            ok: dict[str, ArticleScore] = {}
            bad: dict[str, ScoreFailure] = {}
            for part in (batch[:half], batch[half:]):
                part_ok, part_bad = await self._score_batch(part, query, state, attempt)
                ok.update(part_ok)
                bad.update(part_bad)
            return ok, bad
        return {}, {batch[0].id: _failure(batch[0], error, attempt)}


# ---- module helpers -------------------------------------------------------------------


@dataclass(slots=True)
class _RunState:
    """Shared across the batches of one run so a run-wide failure stops the rest."""

    semaphore: asyncio.Semaphore
    fatal: ScoreItemError | None = None


_RETRYABLE = (TimeoutError, ConnectionError, OSError)
_FATAL = (ImportError,)


def _classify(exc: BaseException) -> ScoreItemError:
    """Turn any exception into a :class:`ScoreItemError` with a retry verdict.

    A scorer that already knows better raises :class:`ScoreItemError` itself and is
    passed through untouched.
    """
    if isinstance(exc, ScoreItemError):
        return exc
    return ScoreItemError(
        safe_str(exc),
        error_type=error_slug(type(exc).__name__),
        retryable=isinstance(exc, _RETRYABLE),
        fatal=isinstance(exc, _FATAL),
    )


def _failure(article: Article, error: ScoreItemError, attempts: int) -> ScoreFailure:
    return ScoreFailure(
        article_id=article.id,
        source=article.source,
        error_type=error.error_type,
        message=safe_str(error),
        attempts=attempts,
    )


def _backoff(base: float, attempt: int) -> float:
    """Exponential backoff with jitter, so retries of one run do not arrive together."""
    return base * (2 ** (attempt - 1)) * (0.5 + random.random()) if base > 0 else 0.0


def _summarise(failures: Iterable[ScoreFailure]) -> list[str]:
    """One human-readable line per distinct failure, with a count."""
    tally: dict[tuple[str, str], int] = {}
    for failure in failures:
        key = (failure.error_type, failure.message)
        tally[key] = tally.get(key, 0) + 1
    return [
        f"{count} article(s) failed to score [{error_type}]: {message}"
        for (error_type, message), count in tally.items()
    ]


def _mark_cache_hit(score: ArticleScore, hit: bool) -> ArticleScore:
    """Stamp where this score came from. Refers to newsscore's own local cache: a hit
    means no request was made, not that a provider served a cached completion."""
    labels = dict(score.labels)
    labels["local_cache_hit"] = hit
    return ArticleScore(
        score=score.score,
        confidence=score.confidence,
        relevance=score.relevance,
        labels=labels,
        expected_impact=score.expected_impact,
    )


def _run_usage(scored: Sequence[ScoredArticle]) -> dict[str, int]:
    """API usage spent by this run: freshly scored articles only.

    Articles served from the local cache cost nothing now, whatever they cost when
    they were first computed, so they must not be billed to this run.
    """
    totals: dict[str, int] = {}
    requests = 0
    for item in scored:
        if item.score.labels.get("local_cache_hit"):
            continue
        usage = item.score.labels.get("usage") or {}
        if not isinstance(usage, Mapping):
            continue
        requests += 1
        for key, value in usage.items():
            try:
                totals[key] = totals.get(key, 0) + int(value)
            except (TypeError, ValueError):
                continue
    if requests:
        totals["requests"] = requests
    return totals


def _status(
    counts: RunCounts, scored: Sequence[ScoredArticle], total_weight: float | None = None
) -> str:
    """Which :class:`RunStatus` describes this run. See that class for the meanings.

    ``total_weight`` comes from the aggregator when it reports one, so decay, lookback
    and impact all count toward "is there any usable evidence". A custom aggregator
    that reports nothing falls back to the per-article weights.
    """
    if counts.submitted == 0:
        return RunStatus.NO_ARTICLES
    if counts.scored == 0:
        return RunStatus.ALL_FAILED
    usable = total_weight > 0 if total_weight is not None else any(i.score.weight > 0 for i in scored)
    if not usable:
        return RunStatus.NO_WEIGHT  # scored fine, but 0.0 here means "no evidence"
    return RunStatus.PARTIAL if counts.failed else RunStatus.OK


def _window(days: float, since: datetime | None, until: datetime | None) -> tuple[datetime, datetime]:
    until = to_utc(until) if until else utcnow()
    since = to_utc(since) if since else until - timedelta(days=days)
    if since > until:
        raise ValueError("since must be before until")
    return since, until


def _dedupe(articles: Iterable[Article]) -> list[Article]:
    """Drop exact duplicates and syndicated copies (same normalised title), keep the earliest."""
    by_title: dict[str, Article] = {}
    seen_ids: set[str] = set()
    for article in sorted(articles, key=lambda a: a.published):
        if article.id in seen_ids:
            continue
        seen_ids.add(article.id)
        key = _NON_ALNUM.sub(" ", article.title.lower()).strip()
        if key and key in by_title:
            continue
        by_title[key or article.id] = article
    return sorted(by_title.values(), key=lambda a: a.published, reverse=True)


def _infer_name(fn: ScoreFn) -> str | None:
    return scorer_name(fn)


def _run(coro: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    coro.close()
    raise RuntimeError("an event loop is already running; use the async method (ascore/afetch) instead")
