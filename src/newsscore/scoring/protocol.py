"""The scoring-function contract.

A *scoring function* is any callable the engine can hand a batch of articles to.
You can pass your own to :class:`~newsscore.NewsScorer` as ``score_fn``.

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

    * an :class:`~newsscore.ArticleScore`;
    * a bare number in ``[-1, 1]`` (confidence and relevance default to ``1.0``);
    * a mapping with key ``"score"`` and optional ``"confidence"``,
      ``"relevance"`` and ``"labels"``;
    * a :class:`ScoreItemError` to fail *that article alone* while its siblings
      in the same batch succeed.

    The function may be plain or ``async``.

Failure
    Returning a :class:`ScoreItemError` for an item is the precise way to report
    one bad article: everything else in the batch is kept and cached.

    Raising instead fails the whole batch. The engine recovers what it can by
    retrying retryable errors and then splitting the batch to isolate the
    offending article, but that costs extra calls, so prefer returning.

Optional attributes
    ``name``: a short stable string used as the cache key for this scorer. Give
    your function one (``my_scorer.name = "v2"``) or pass ``scorer_name=`` to
    the engine. Lambdas without a name are not cached.
"""

from __future__ import annotations

import inspect
import re
from typing import Any, Awaitable, Callable, Mapping, Sequence, Union

from ..models import Article, ArticleScore

_CAMEL_WORD = re.compile(r"(.)([A-Z][a-z]+)")
_CAMEL_RUN = re.compile(r"([a-z0-9])([A-Z])")


def error_slug(name: str) -> str:
    """``TypeSafeAPITimeoutError`` -> ``api_timeout``: a short stable key for grouping
    failures. Keeps acronyms whole, so it is not ``a_p_i_timeout``."""
    name = name.removeprefix("TypeSafe").removesuffix("Error") or "error"
    split = lambda match: match.group(1) + "_" + match.group(2)  # noqa: E731
    return _CAMEL_RUN.sub(split, _CAMEL_WORD.sub(split, name)).lower()


def safe_str(exc: BaseException) -> str:
    """``str(exc)`` without trusting it — a half-built exception can raise from
    ``__str__``, and the error path is the one place that must not throw."""
    try:
        return str(exc) or type(exc).__name__
    except Exception:
        return type(exc).__name__


class ScorerUnavailable(RuntimeError):
    """A scorer cannot run in this environment: missing dependency or credentials.

    Raised at construction, so explicitly asking for a scorer fails immediately and
    loudly instead of silently producing zeros or falling back to a different model.
    """


class ScoreItemError(Exception):
    """One article could not be scored.

    Return one of these from a scoring function in place of an article's score to
    fail that article on its own; raise it to fail the whole batch.

    Args:
        message: What went wrong, shown in diagnostics.
        error_type: Short stable slug for grouping failures (``"timeout"``,
            ``"rate_limit"``, ``"auth"``, ``"bad_request"``...). Avoid free text.
        retryable: Another attempt might succeed (a timeout, a 5xx, a rate limit).
            The engine retries these a bounded number of times; anything else is
            recorded immediately.
        fatal: The condition applies to every other article too — bad credentials,
            a missing dependency — so the run should stop instead of repeating the
            same failure once per article.
    """

    def __init__(
        self, message: str, *, error_type: str = "unknown", retryable: bool = False, fatal: bool = False
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.retryable = retryable
        self.fatal = fatal


ScoreItem = Union[ArticleScore, float, int, Mapping[str, Any], ScoreItemError]
ScoreOutput = Sequence[ScoreItem]
ScoreFn = Callable[[Sequence[Article], str], Union[ScoreOutput, Awaitable[ScoreOutput]]]


def normalise_scores(raw: Any, expected: int) -> list[Union[ArticleScore, ScoreItemError]]:
    """Coerce any accepted scorer output into one outcome per article, in order.

    Items that are :class:`ScoreItemError` pass through untouched, so a scorer can
    fail single articles without sinking the batch. Raises ``ValueError`` with a
    precise message on wrong length or type, so a misbehaving user scorer fails
    loudly instead of silently mis-aligning scores.
    """
    if isinstance(raw, (str, bytes, Mapping)) or not isinstance(raw, Sequence):
        raise ValueError(
            f"scorer must return a sequence of {expected} items, got {type(raw).__name__}"
        )
    if len(raw) != expected:
        raise ValueError(f"scorer returned {len(raw)} items for {expected} articles")
    return [_coerce(item, index) for index, item in enumerate(raw)]


def _coerce(item: Any, index: int) -> Union[ArticleScore, ScoreItemError]:
    if isinstance(item, (ArticleScore, ScoreItemError)):
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


async def call_score_fn(
    fn: ScoreFn, articles: Sequence[Article], query: str
) -> list[Union[ArticleScore, ScoreItemError]]:
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
