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
from typing import Any, Sequence

from ..models import Article, ArticleScore

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

    async def __call__(self, articles: Sequence[Article], query: str) -> list[ArticleScore]:
        return list(await asyncio.gather(*(self._score_one(a, query) for a in articles)))

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


def _unit(value: Any) -> float:
    return max(0.0, min(1.0, float(value)))
