"""Score articles with TypeSafe AI's Jev "System One" model.

Jev does not generate text; it answers typed questions with calibrated
probabilities. One ``system_one`` call per article asks four questions:

======================  =========  ==========================================================
question                type       becomes
======================  =========  ==========================================================
``sentiment``           Score(5)   ``score``: probability-weighted level, rescaled to [-1, 1]
``relevant``            Noul       ``relevance``
``category``            Choice     ``labels["category"]``, ``labels["category_probs"]``
``novel``               Noul       ``labels["novel"]`` (new information vs. rehash)
======================  =========  ==========================================================

``confidence`` is Jev's own calibrated confidence for the sentiment answer.
Requires ``pip install newsscore[jev]`` and ``TYPESAFE_API_KEY``.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
from typing import Any, Sequence

from ..models import Article, ArticleScore
from .protocol import ScoreItemError, ScorerUnavailable, error_slug, safe_str

# Jev's error taxonomy, matched by class name so this module never imports the
# optional SDK just to classify. Names are checked across the whole MRO, so
# subclasses (TypeSafeAPITimeoutError < TypeSafeAPIConnectionError) resolve too.
RETRYABLE_ERRORS = frozenset(
    {
        "TypeSafeRateLimitError",
        "TypeSafeInternalServerError",
        "TypeSafeAPITimeoutError",
        "TypeSafeAPIConnectionError",
    }
)
# These will fail identically for every other article, so the run stops instead of
# repeating the same rejection once per headline.
FATAL_ERRORS = frozenset({"TypeSafeAuthenticationError", "TypeSafePermissionDeniedError"})

SENTIMENT_LEVELS = [
    "Clearly negative for the company's stock: losses, misses, downgrades, lawsuits, guidance cuts.",
    "Mildly negative: headwinds, concerns, softer outlook, minor setbacks.",
    "Neutral or mixed: factual, balanced, or unrelated to the company's value.",
    "Mildly positive: modest beats, upgrades, favourable developments.",
    "Clearly positive for the company's stock: strong beats, raised guidance, major wins.",
]

CATEGORIES = {
    "earnings": "Quarterly or annual results, revenue, EPS, margins.",
    "guidance": "Forward-looking outlook, forecasts, targets set by the company.",
    "m_and_a": "Mergers, acquisitions, divestitures, strategic investments.",
    "legal_regulatory": "Lawsuits, investigations, fines, regulatory approvals or blocks.",
    "product": "Product launches, recalls, technology, partnerships, customers.",
    "analyst": "Analyst ratings, price targets, research notes.",
    "management": "Executive changes, governance, insider activity, layoffs.",
    "macro_sector": "Market-wide or sector news that mentions the company incidentally.",
    "other": "Anything else.",
}


class JevScorer:
    """A :data:`~newsscore.scoring.protocol.ScoreFn` backed by Jev.

    Args:
        api_key: Overrides ``TYPESAFE_API_KEY``.
        model: Overrides the SDK default (``jev-latest``).
        concurrency: Max in-flight Jev requests.
        timeout: Per-request timeout in seconds.
        client: An existing ``AsyncTypeSafeClient`` to reuse instead of creating one.
    """

    name = "jev-v1"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        *,
        concurrency: int = 8,
        timeout: float = 30.0,
        client: Any | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        self._client = client
        self._semaphore = asyncio.Semaphore(concurrency)
        self._questions: dict[str, Any] | None = None
        if client is None:
            self.preflight()

    def preflight(self) -> None:
        """Fail now if Jev cannot run here, rather than returning zeros later.

        Asking for Jev explicitly and getting a silent 0.0 back is indistinguishable
        from genuinely neutral news, so the dependency and the credential are checked
        at construction. :func:`~newsscore.scoring.default_scorer` still falls back to
        the keyword scorer, but only when Jev was *not* asked for by name.

        Deliberately cheap: it locates the SDK without importing it and looks for a
        key, so a fully cached run never pays to build a client it will not call. A
        key that exists but is rejected surfaces on the first request instead, as a
        fatal error that stops the run with the provider's own message.
        """
        if importlib.util.find_spec("typesafe_sdk") is None:
            raise ScorerUnavailable(
                "the Jev scorer needs the TypeSafe SDK: pip install 'newsscore[jev]'"
            )
        if not (self._api_key or os.environ.get("TYPESAFE_API_KEY")):
            raise ScorerUnavailable(
                "the Jev scorer needs an API key: pass api_key= or set TYPESAFE_API_KEY"
            )

    # ---- setup -------------------------------------------------------------------

    def _sdk(self) -> Any:
        try:
            import typesafe_sdk
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise ImportError(
                "The Jev scorer needs the TypeSafe SDK: pip install 'newsscore[jev]'"
            ) from exc
        return typesafe_sdk

    def _get_client(self) -> Any:
        if self._client is None:
            sdk = self._sdk()
            self._client = sdk.AsyncTypeSafeClient(
                api_key=self._api_key, model=self._model, timeout=self._timeout
            )
        return self._client

    def _get_questions(self) -> dict[str, Any]:
        if self._questions is None:
            sdk = self._sdk()
            self._questions = {
                "sentiment": sdk.Score(
                    instructions=(
                        "How would an investor in the company named in `query` read this news? "
                        "Judge the implication for the stock, not the general mood of the text."
                    ),
                    criteria=SENTIMENT_LEVELS,
                ),
                "relevant": sdk.Noul(
                    instructions="Is this article materially about the company or asset named in `query`, "
                    "rather than mentioning it in passing?"
                ),
                "category": sdk.Choice(
                    instructions="What kind of news is this, for the company in `query`?",
                    criteria=CATEGORIES,
                ),
                "novel": sdk.Noul(
                    instructions="Does the article contain new information (an event, number or decision), "
                    "as opposed to commentary or a rehash of earlier news?"
                ),
            }
        return self._questions

    # ---- scoring -----------------------------------------------------------------

    async def __call__(
        self, articles: Sequence[Article], query: str
    ) -> list[ArticleScore | ScoreItemError]:
        """One Jev call per article, each failing on its own.

        Returns a :class:`~newsscore.ScoreItemError` in place of any article whose
        request failed, so the rest of the batch is kept and cached.
        """
        results = await asyncio.gather(
            *(self._score_one(a, query) for a in articles), return_exceptions=True
        )
        out: list[ArticleScore | ScoreItemError] = []
        for result in results:
            if isinstance(result, asyncio.CancelledError):
                raise result
            out.append(classify_error(result) if isinstance(result, BaseException) else result)
        return out

    async def _score_one(self, article: Article, query: str) -> ArticleScore:
        state = {
            "query": query,
            "title": article.title,
            "summary": article.summary or "",
            "source": article.source,
            "published": article.published.isoformat(),
            "symbols": list(article.symbols),
        }
        async with self._semaphore:
            response = await self._get_client().system_one(state=state, questions=self._get_questions())
        return self._convert(response)

    @staticmethod
    def _convert(response: Any) -> ArticleScore:
        answers = response.answers
        sentiment = answers["sentiment"]
        probs = dict(getattr(sentiment, "probabilities", None) or {})
        n_levels = len(SENTIMENT_LEVELS)
        if probs:
            expected = sum(float(p) * int(level) for level, p in probs.items()) / max(sum(probs.values()), 1e-9)
        else:
            expected = float(sentiment.score)
        score = max(-1.0, min(1.0, expected / (n_levels - 1) * 2.0 - 1.0))

        category = answers["category"]
        return ArticleScore(
            score=score,
            confidence=_unit(getattr(sentiment, "confidence", 1.0)),
            relevance=_unit(answers["relevant"].noul),
            labels={
                "scorer": JevScorer.name,
                "model": getattr(response, "model", None),
                "sentiment_probs": {int(k): float(v) for k, v in probs.items()},
                "category": category.choice,
                "category_probs": {k: float(v) for k, v in (category.probabilities or {}).items()},
                "novel": _unit(answers["novel"].noul),
            },
        )

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is not None and hasattr(client, "aclose"):
            await client.aclose()


def classify_error(exc: BaseException) -> ScoreItemError:
    """Turn one Jev SDK exception into a retry verdict for the engine."""
    names = {cls.__name__ for cls in type(exc).__mro__}
    return ScoreItemError(
        safe_str(exc),
        error_type=error_slug(type(exc).__name__),
        retryable=bool(names & RETRYABLE_ERRORS) or isinstance(exc, (TimeoutError, ConnectionError)),
        fatal=bool(names & FATAL_ERRORS) or isinstance(exc, ImportError),
    )


def _unit(value: Any) -> float:
    return max(0.0, min(1.0, float(value)))
