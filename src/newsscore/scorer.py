"""The engine: sources in, scoring function in the middle, one result out."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Sequence

import httpx

from .aggregate import AggregateFn, make_aggregator
from .cache import ScoreCache
from .config import SourceStore, load_env
from .http import make_client
from .models import Article, ArticleScore, ScoredArticle, ScoreResult, utcnow
from .scoring import ScoreFn, call_score_fn, default_scorer, make_scorer, scorer_name
from .sources import NewsSource, SourceError, make_source
from .sources.base import to_utc

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
        aggregate_fn: Replace the default aggregation entirely.
        batch_size: Articles handed to ``score_fn`` per call.
        concurrency: Max concurrent ``score_fn`` calls.
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
        aggregate_fn: AggregateFn | None = None,
        batch_size: int = 16,
        concurrency: int = 4,
        timeout: float = 20.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.score_fn: ScoreFn = (
            default_scorer() if score_fn is None else make_scorer(score_fn) if isinstance(score_fn, str) else score_fn
        )
        self.scorer_name = scorer_name or _infer_name(self.score_fn)
        self.aggregate_fn = aggregate_fn or make_aggregator(half_life_hours)
        self.batch_size = max(1, batch_size)
        self.concurrency = max(1, concurrency)
        self.timeout = timeout
        self._http_client = http_client
        self._sources: dict[str, NewsSource] = {}

        self.cache: ScoreCache | None = None
        if cache and self.scorer_name is None:
            log.warning("score_fn has no stable name; caching disabled (set scorer_name= to enable)")
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
        articles, errors = await self._fetch(query, since, until, sources)
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
        articles, errors = await self._fetch(query, since, until, sources)
        scored, score_errors = await self._score(articles, query)
        errors.extend(score_errors)
        agg = self.aggregate_fn(scored, until)
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
        )

    def fetch(self, query: str, **kwargs: Any) -> list[Article]:
        """Blocking version of :meth:`afetch`."""
        return _run(self.afetch(query, **kwargs))

    def score(self, query: str, **kwargs: Any) -> ScoreResult:
        """Blocking version of :meth:`ascore`."""
        return _run(self.ascore(query, **kwargs))

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
    ) -> tuple[list[Article], list[str]]:
        selected = self._select(names)
        if not selected:
            return [], ["no sources registered; add one with source_add() or `newsscore source add`"]

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
        return _dedupe(articles), errors

    async def _score(self, articles: Sequence[Article], query: str) -> tuple[list[ScoredArticle], list[str]]:
        if not articles:
            return [], []
        scores: dict[str, ArticleScore] = {}
        if self.cache is not None and self.scorer_name:
            scores.update(self.cache.get_many(self.scorer_name, query, (a.id for a in articles)))
        pending = [a for a in articles if a.id not in scores]
        errors: list[str] = []

        semaphore = asyncio.Semaphore(self.concurrency)

        async def run(batch: Sequence[Article]) -> dict[str, ArticleScore] | str:
            async with semaphore:
                try:
                    result = await call_score_fn(self.score_fn, batch, query)
                except Exception as exc:  # a bad batch must not sink the run
                    return f"scorer failed on {len(batch)} article(s): {exc}"
            return {a.id: s for a, s in zip(batch, result)}

        batches = [pending[i : i + self.batch_size] for i in range(0, len(pending), self.batch_size)]
        fresh: dict[str, ArticleScore] = {}
        for outcome in await asyncio.gather(*(run(b) for b in batches)):
            if isinstance(outcome, str):
                errors.append(outcome)
            else:
                fresh.update(outcome)
        if fresh and self.cache is not None and self.scorer_name:
            self.cache.put_many(self.scorer_name, query, fresh)
        scores.update(fresh)

        scored = [ScoredArticle(a, scores[a.id]) for a in articles if a.id in scores]
        return scored, errors


# ---- module helpers -------------------------------------------------------------------


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
