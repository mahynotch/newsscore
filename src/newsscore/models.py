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
        }
        if include_articles:
            data["articles"] = [a.to_dict() for a in self.articles]
        return data


def utcnow() -> datetime:
    return datetime.now(tz=UTC)
