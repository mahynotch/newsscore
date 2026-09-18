"""Core data types shared by sources, scorers and the engine.

Everything here is a plain dataclass with no I/O, so it is cheap to construct,
easy to serialise and safe to pass across threads or tasks.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

UTC = timezone.utc


def to_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def parse_dt(value: Any) -> datetime:
    """Parse the timestamp formats seen across news APIs into aware UTC.

    Handles unix seconds/milliseconds, ISO 8601 (with ``Z``), Alpha Vantage's
    ``YYYYMMDDTHHMMSS`` and RFC 2822 (RSS ``pubDate``).
    """
    if isinstance(value, datetime):
        return to_utc(value)
    if isinstance(value, (int, float)):
        seconds = value / 1000.0 if value > 1e11 else value
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    text = str(value).strip()
    if text.isdigit():
        return parse_dt(int(text))
    if len(text) == 15 and text[8] == "T" and text[:8].isdigit():
        return datetime.strptime(text, "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
    try:
        return to_utc(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        pass
    from email.utils import parsedate_to_datetime  # deferred: ~30 ms, RSS pubDate only

    try:
        return to_utc(parsedate_to_datetime(text))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"unrecognised timestamp: {value!r}") from exc


def make_id(*parts: str) -> str:
    """Deterministic short id from a handful of strings (source, url, title...)."""
    digest = hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()
    return digest[:16]


@dataclass(frozen=True, slots=True)
class Article:
    """One news item as returned by a :class:`~newsscore.sources.NewsSource`.

    Attributes:
        id: Stable identifier, unique per source (used for caching and de-duplication).
        source: Name of the source instance that produced it, e.g. ``"finnhub"``.
        title: Headline.
        published: Publication time, timezone-aware, in UTC.
        url: Link to the article, if the source provides one.
        summary: Short body text or description, if available.
        symbols: Ticker symbols the source associates with the article.
        raw: The untouched provider payload, for anyone who needs a vendor field.
    """

    id: str
    source: str
    title: str
    published: datetime
    url: str | None = None
    summary: str | None = None
    symbols: tuple[str, ...] = ()
    raw: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def text(self) -> str:
        """Title plus summary: the text most scorers will want to read."""
        return f"{self.title}\n\n{self.summary}" if self.summary else self.title

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "title": self.title,
            "published": self.published.isoformat(),
            "url": self.url,
            "summary": self.summary,
            "symbols": list(self.symbols),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Article":
        """Rebuild an article from JSON, for news collected by another application.

        ``title`` and ``published`` are required; everything else is optional.
        ``published`` accepts anything :func:`parse_dt` understands. The caller's
        own ``id`` is preserved when given — it is what scores are cached against —
        and derived from source plus url or title only when absent, matching what a
        built-in source would have produced.
        """
        try:
            title = str(data["title"])
            published = parse_dt(data["published"])
        except KeyError as exc:
            raise ValueError(f"article is missing required field {exc.args[0]!r}") from exc
        except (TypeError, ValueError) as exc:
            raise ValueError(f"article {data.get('id') or title!r}: {exc}") from exc
        source = str(data.get("source") or "external")
        url = data.get("url") or None
        article_id = data.get("id") or make_id(source, url or f"{title}|{published.isoformat()}")
        symbols = data.get("symbols") or ()
        if isinstance(symbols, str):
            symbols = [symbols]
        return cls(
            id=str(article_id),
            source=source,
            title=title.strip(),
            published=published,
            url=url,
            summary=(data.get("summary") or "").strip() or None,
            symbols=tuple(str(sym).upper() for sym in symbols if sym),
            raw=dict(data.get("raw") or {}),
        )


@dataclass(frozen=True, slots=True)
class ArticleScore:
    """The verdict of a scoring function on one article.

    Attributes:
        score: Sentiment in ``[-1.0, 1.0]``; negative is bearish, positive is bullish.
        confidence: ``[0.0, 1.0]``, how sure the scorer is. Used as an aggregation weight.
        relevance: ``[0.0, 1.0]``, how much the article is actually about the query.
            Also used as an aggregation weight. Defaults to fully relevant.
        labels: Free-form extras (event category, probabilities, model name...).
    """

    score: float
    confidence: float = 1.0
    relevance: float = 1.0
    labels: Mapping[str, Any] = field(default_factory=dict, compare=False)

    def __post_init__(self) -> None:
        if not -1.0 <= self.score <= 1.0:
            raise ValueError(f"score must be in [-1, 1], got {self.score!r}")
        for name in ("confidence", "relevance"):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be in [0, 1], got {value!r}")

    @property
    def weight(self) -> float:
        """Confidence times relevance: how much this score should count."""
        return self.confidence * self.relevance

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "confidence": self.confidence,
            "relevance": self.relevance,
            "labels": dict(self.labels),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ArticleScore":
        return cls(
            score=float(data["score"]),
            confidence=float(data.get("confidence", 1.0)),
            relevance=float(data.get("relevance", 1.0)),
            labels=dict(data.get("labels") or {}),
        )


@dataclass(frozen=True, slots=True)
class ScoredArticle:
    article: Article
    score: ArticleScore

    def to_dict(self) -> dict[str, Any]:
        return {**self.article.to_dict(), **self.score.to_dict()}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ScoredArticle":
        """Inverse of :meth:`to_dict`, so saved results can be re-aggregated offline."""
        return cls(article=Article.from_dict(data), score=ArticleScore.from_dict(data))


@dataclass(frozen=True, slots=True)
class ScoreFailure:
    """One scoring task that did not produce a score.

    A *task* is one ``(article, query)`` pair, so the same article scored for two
    targets can fail independently.
    """

    article_id: str
    source: str
    error_type: str
    message: str
    attempts: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "article_id": self.article_id,
            "source": self.source,
            "error_type": self.error_type,
            "message": self.message,
            "attempts": self.attempts,
        }


class RunStatus:
    """How a run ended. One value, chosen by the precedence listed in :data:`ORDER`.

    ``OK`` covers a successful run whose aggregate happens to be 0.0 — a genuine
    neutral reading. ``NO_WEIGHT`` is the different case where articles scored fine
    but nothing carried usable weight (everything stale, irrelevant or zero
    confidence), so 0.0 means "no evidence", not "neutral evidence". Never treat
    the two as the same number.
    """

    NO_ARTICLES = "no_articles"  # nothing to score
    ALL_FAILED = "all_failed"  # every scoring task failed
    NO_WEIGHT = "no_weight"  # scored, but total aggregation weight is zero
    PARTIAL = "partial"  # some tasks failed, some succeeded with weight
    OK = "ok"  # every task succeeded

    ORDER = (NO_ARTICLES, ALL_FAILED, NO_WEIGHT, PARTIAL, OK)
    FAILED = (NO_ARTICLES, ALL_FAILED, NO_WEIGHT)


@dataclass(frozen=True, slots=True)
class RunCounts:
    """Reconcilable tally for one run, in scoring tasks.

    ``fetched == deduplicated + filtered + submitted`` and
    ``submitted == scored + failed``, so every item handed in can be accounted for.

    Attributes:
        fetched: Items the run started from — returned by sources, or handed to
            :meth:`~newsscore.NewsScorer.score_articles` by the caller.
        deduplicated: Dropped as duplicates of another item.
        filtered: Dropped for falling outside the window, including anything dated
            after ``as_of``.
        submitted: Actually handed to the scorer.
        scored: Produced a score.
        failed: Did not produce a score; see ``ScoreResult.failures``.
    """

    fetched: int = 0
    deduplicated: int = 0
    filtered: int = 0
    submitted: int = 0
    scored: int = 0
    failed: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "fetched": self.fetched,
            "deduplicated": self.deduplicated,
            "filtered": self.filtered,
            "submitted": self.submitted,
            "scored": self.scored,
            "failed": self.failed,
        }


@dataclass(slots=True)
class ScoreResult:
    """Aggregate sentiment for one query over one time window.

    Attributes:
        query: The symbol or keyword that was scored.
        since / until: The time window that was fetched.
        score: Weighted aggregate sentiment in ``[-1, 1]``.
        confidence: ``[0, 1]``, saturating in the amount of weighted evidence.
        n_articles: Number of articles that received a score.
        by_source: Aggregate score per source name.
        articles: Every scored article, newest first.
        errors: Human-readable messages for sources or batches that failed.
        status: One of :class:`RunStatus`. Check this before trusting ``score``:
            a 0.0 with ``status="no_weight"`` or ``"all_failed"`` is not neutral news.
        counts: Reconcilable tally of scoring tasks (see :class:`RunCounts`).
        failures: One :class:`ScoreFailure` per task that did not produce a score.
            Failed articles are never aggregated as neutral evidence.
    """

    query: str
    since: datetime
    until: datetime
    score: float
    confidence: float
    n_articles: int
    by_source: dict[str, float] = field(default_factory=dict)
    articles: list[ScoredArticle] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    status: str = RunStatus.OK
    counts: RunCounts = field(default_factory=RunCounts)
    failures: list[ScoreFailure] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when every scoring task succeeded and the aggregate rests on real weight."""
        return self.status == RunStatus.OK

    def to_dict(self, *, include_articles: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "query": self.query,
            "since": self.since.isoformat(),
            "until": self.until.isoformat(),
            "score": self.score,
            "confidence": self.confidence,
            "n_articles": self.n_articles,
            "by_source": dict(self.by_source),
            "errors": list(self.errors),
            "status": self.status,
            "counts": self.counts.to_dict(),
            "failures": [f.to_dict() for f in self.failures],
        }
        if include_articles:
            data["articles"] = [a.to_dict() for a in self.articles]
        return data


def utcnow() -> datetime:
    return datetime.now(tz=UTC)
