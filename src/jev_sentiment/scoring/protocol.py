"""The scoring-function contract.

A *scoring function* is any callable the engine can hand a batch of articles to.
You can pass your own to :class:`~jev_sentiment.NewsScorer` as ``score_fn``.

Contract
--------

Signature::

    def my_scorer(articles: Sequence[Article], query: str) -> ScoreOutput
    async def my_scorer(articles: Sequence[Article], query: str) -> ScoreOutput

Input
    ``articles``: the batch to score (default up to 16 items, see ``batch_size``).
    ``query``: the symbol or keyword the articles were fetched for, so the scorer
    can judge relevance.

Output
    A sequence with **exactly one item per input article, in the same order**.
    Each item may be any of:

    * an :class:`~jev_sentiment.ArticleScore`;
    * a bare number in ``[-1, 1]`` (confidence and relevance default to ``1.0``);
    * a mapping with key ``"score"`` and optional ``"confidence"``,
      ``"relevance"`` and ``"labels"``.

    The function may be plain or ``async``. Raising an exception fails only the
    current batch; the engine records the message in ``ScoreResult.errors`` and
    continues with the other batches.

Optional attributes
    ``name``: a short stable string used as the cache key for this scorer. Give
    your function one (``my_scorer.name = "v2"``) or pass ``scorer_name=`` to
    the engine. Lambdas without a name are not cached.
"""

from __future__ import annotations

import inspect
from typing import Any, Awaitable, Callable, Mapping, Sequence, Union

from ..models import Article, ArticleScore

ScoreItem = Union[ArticleScore, float, int, Mapping[str, Any]]
ScoreOutput = Sequence[ScoreItem]
ScoreFn = Callable[[Sequence[Article], str], Union[ScoreOutput, Awaitable[ScoreOutput]]]


def normalise_scores(raw: Any, expected: int) -> list[ArticleScore]:
    """Coerce any accepted scorer output into ``list[ArticleScore]``.

    Raises ``ValueError`` with a precise message on wrong length or type, so a
    misbehaving user scorer fails loudly instead of silently mis-aligning scores.
    """
    if isinstance(raw, (str, bytes, Mapping)) or not isinstance(raw, Sequence):
        raise ValueError(
            f"scorer must return a sequence of {expected} items, got {type(raw).__name__}"
        )
    if len(raw) != expected:
        raise ValueError(f"scorer returned {len(raw)} items for {expected} articles")
    return [_coerce(item, index) for index, item in enumerate(raw)]


def _coerce(item: Any, index: int) -> ArticleScore:
    if isinstance(item, ArticleScore):
        return item
    if isinstance(item, bool):  # bool is an int subclass; almost certainly a mistake
        raise ValueError(f"item {index}: got a bool, expected a score in [-1, 1]")
    if isinstance(item, (int, float)):
        return ArticleScore(score=float(item))
    if isinstance(item, Mapping):
        try:
            return ArticleScore.from_dict(item)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"item {index}: {exc}") from exc
    raise ValueError(f"item {index}: unsupported type {type(item).__name__}")


async def call_score_fn(fn: ScoreFn, articles: Sequence[Article], query: str) -> list[ArticleScore]:
    """Invoke a sync or async scoring function and normalise its output."""
    result = fn(articles, query)
    if inspect.isawaitable(result):
        result = await result
    return normalise_scores(result, len(articles))


def per_article(fn: Callable[[Article, str], Union[ScoreItem, Awaitable[ScoreItem]]]) -> ScoreFn:
    """Adapt a one-article-at-a-time function into a batch :data:`ScoreFn`.

    Handy when your model has no batching anyway::

        scorer = NewsScorer(score_fn=per_article(lambda a, q: my_model(a.text)))
    """

    async def batch(articles: Sequence[Article], query: str) -> list[ScoreItem]:
        out: list[ScoreItem] = []
        for article in articles:
            item = fn(article, query)
            if inspect.isawaitable(item):
                item = await item
            out.append(item)
        return out

    batch.name = getattr(fn, "name", None) or getattr(fn, "__qualname__", "per_article")  # type: ignore[attr-defined]
    batch.__doc__ = fn.__doc__
    return batch


def scorer_name(fn: Any) -> str | None:
    """Best stable name for caching, or ``None`` if the callable is anonymous."""
    name = getattr(fn, "name", None)
    if isinstance(name, str) and name:
        return name
    qualname = getattr(fn, "__qualname__", None) or type(fn).__qualname__
    if "<lambda>" in qualname or "<locals>" in qualname:
        return None
    module = getattr(fn, "__module__", None) or type(fn).__module__
    return f"{module}.{qualname}"
