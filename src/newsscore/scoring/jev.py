"""Score articles with TypeSafe AI's Jev "System One" model.

Jev does not generate text; it answers typed questions with calibrated
probabilities. One ``system_one`` call per article asks four questions:

======================  =========  ==========================================================
question                type       becomes
======================  =========  ==========================================================
``sentiment``           Score(5)   ``score``: probability-weighted level, rescaled to [-1, 1]
``relevant``            Noul       ``relevance``
``impact``              Choice(3)  ``expected_impact``, ``labels["impact_probs"]``
``category``            Choice     ``labels["category"]``, ``labels["category_probs"]``
``novel``               Noul       ``labels["novel"]`` (new information vs. rehash)
======================  =========  ==========================================================

``confidence`` is Jev's own calibrated confidence for the sentiment answer.

Sentiment and impact are asked separately on purpose. Sentiment is *direction*, impact
is *materiality* over :data:`DEFAULT_HORIZON`, and neither follows from the other: a
lawsuit can be strongly bearish and high impact, while a routine contract win is mildly
bullish and low impact. The impact rubric says so explicitly, because a model asked
casually will otherwise read impact off the strength of the sentiment.

All five questions ride in one ``system_one`` call, so impact costs no extra request.
Requires ``pip install newsscore[jev]`` and ``TYPESAFE_API_KEY``.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import re
import time
from typing import Any, Mapping, Sequence

from ..models import IMPACT_LEVELS, Article, ArticleScore
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

# The model this scoring contract was written and checked against. Pinned on purpose:
# `jev-latest` resolves to some concrete version server-side, and you only learn which
# one *after* paying for the call, so it can never be part of a cache lookup. Asking
# for an alias is allowed and switches caching off rather than risking answers from an
# unknown model. Raise this deliberately, and expect scores to move when you do.
DEFAULT_MODEL = "jev-1.13.0"
_PINNED = re.compile(r"\d+\.\d+")


def is_pinned(model: str) -> bool:
    """True for a concrete version like ``jev-1.13.0``, false for ``jev-latest``."""
    return bool(_PINNED.search(model))

SENTIMENT_LEVELS = [
    "Clearly negative for the company's stock: losses, misses, downgrades, lawsuits, guidance cuts.",
    "Mildly negative: headwinds, concerns, softer outlook, minor setbacks.",
    "Neutral or mixed: factual, balanced, or unrelated to the company's value.",
    "Mildly positive: modest beats, upgrades, favourable developments.",
    "Clearly positive for the company's stock: strong beats, raised guidance, major wins.",
]

#: Default wording for the three impact levels. Override the text with
#: ``JevScorer(impact_rubric=...)``; the three keys themselves are fixed, because
#: ``ArticleScore.expected_impact`` and the impact weight mapping are defined on them.
#: The period the impact question asks about. The quant use case is the potential
#: effect on the stock over the next few trading days, not the article's mood and not
#: a long-run view. Change it with ``JevScorer(horizon=...)``; it is part of the cache
#: fingerprint, so a different horizon never reuses answers given for another one.
DEFAULT_HORIZON = "the next 1 to 5 trading days"

IMPACT_RUBRIC = {
    "low": (
        "Immaterial for the stock: routine, incremental, already widely known or "
        "priced in. A typical investor would not revise their view."
    ),
    "medium": (
        "Somewhat material: a real development that shifts the outlook a little, "
        "but not on its own decisive for the stock."
    ),
    "high": (
        "Material for the stock: the kind of development that moves the price or "
        "changes the investment case."
    ),
}

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
        model: Which Jev version to ask for. Defaults to :data:`DEFAULT_MODEL`, a
            pinned version, so cached scores always belong to a known model. Passing
            a mutable alias such as ``"jev-latest"`` disables caching for this scorer.
        horizon: The period the impact question asks about. Defaults to
            :data:`DEFAULT_HORIZON`. Part of the cache fingerprint.
        impact: Ask the expected-impact question. Off by default: it costs ~24% more
            tokens per article and the label does nothing unless you also switch on
            ``impact_weights`` in aggregation, so the two are opted into together.
            See the benchmark in the README before turning it on.
        impact_rubric: Override the wording of the three levels. The keys stay
            ``low``/``medium``/``high``; only the descriptions change. Part of the
            cache fingerprint, so a reworded rubric never reuses old answers.
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
        horizon: str | None = None,
        impact: bool = False,
        impact_rubric: Mapping[str, str] | None = None,
        concurrency: int = 8,
        timeout: float = 30.0,
        client: Any | None = None,
    ) -> None:
        self._api_key = api_key
        self._impact = impact
        self._impact_rubric = dict(impact_rubric or IMPACT_RUBRIC)
        if set(self._impact_rubric) != set(IMPACT_LEVELS):
            raise ValueError(
                f"impact_rubric must describe exactly {IMPACT_LEVELS}, "
                f"got {sorted(self._impact_rubric)}"
            )
        self._horizon = horizon or DEFAULT_HORIZON
        self._model = model or DEFAULT_MODEL
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None
        self._loop: Any = None  # the loop _client and _semaphore were built on
        self._concurrency = concurrency
        self._semaphore = asyncio.Semaphore(concurrency)
        self._questions: dict[str, Any] | None = None
        if client is None:
            self.preflight()

    @property
    def model(self) -> str:
        """The model version being requested."""
        return self._model

    @property
    def horizon(self) -> str:
        """The period the impact question asks about."""
        return self._horizon

    @property
    def asks_impact(self) -> bool:
        """Whether the expected-impact question is part of the request."""
        return self._impact

    @property
    def fingerprint(self) -> str | None:
        """Digest of everything that can change an answer, or ``None`` to refuse caching.

        Covers the model, and the text of every question, rubric and criterion. Change
        a word of an instruction and previously cached scores stop matching, because
        they were produced by answering a different question.
        """
        if not is_pinned(self._model):
            return None  # an alias could be anything tomorrow; do not cache under it
        contract = {
            "model": self._model,
            "levels": SENTIMENT_LEVELS,
            "categories": CATEGORIES,
            "impact_levels": self._impact_rubric if self._impact else None,
            "horizon": self._horizon,
            "questions": sorted(self._question_spec().items()),
        }
        blob = json.dumps(contract, sort_keys=True, default=str).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()[:16]

    def _question_spec(self) -> dict[str, str]:
        """The instruction text of each question, as it goes to the model."""
        spec: dict[str, str] = {}
        if self._impact:
            spec["impact"] = (
                "How material is this news for the company named in `query`, over "
                f"{self._horizon}? Judge how much it should change an investor's view "
                "of the stock, NOT how good or bad the news is. Strongly negative and "
                "strongly positive news can both be high impact, and mild news of "
                "either sign can be low impact. Routine, incremental or already "
                "widely reported items are low."
            )
        spec.update(
            {
                "sentiment": (
                "How would an investor in the company named in `query` read this news? "
                "Judge the implication for the stock, not the general mood of the text."
            ),
            "relevant": (
                "Is this article materially about the company or asset named in `query`, "
                "rather than mentioning it in passing?"
            ),
                "category": "What kind of news is this, for the company in `query`?",
                "novel": (
                    "Does the article contain new information (an event, number or decision), "
                    "as opposed to commentary or a rehash of earlier news?"
                ),
            }
        )
        return spec

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

    def _rebind(self) -> None:
        """Rebuild the client and semaphore when the running event loop has changed.

        Every synchronous call goes through its own ``asyncio.run``, which closes the
        loop on the way out. A client built on that loop raises ``Event loop is
        closed`` on its next request, so scoring twice through the sync API with one
        scorer would fail the second time. Noticing the change here keeps a reused
        scorer working, which is the ordinary shape of a per-symbol loop.

        The dead client is dropped rather than closed: its transport belongs to a loop
        that is already closed, and awaiting ``aclose`` on it would raise. A client
        handed in by the caller is theirs to manage and is never replaced.
        """
        loop = asyncio.get_running_loop()
        if self._loop is loop:
            return
        if self._owns_client:
            self._client = None
        self._semaphore = asyncio.Semaphore(self._concurrency)
        self._loop = loop

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
            spec = self._question_spec()
            self._questions = {
                "sentiment": sdk.Score(instructions=spec["sentiment"], criteria=SENTIMENT_LEVELS),
                "relevant": sdk.Noul(instructions=spec["relevant"]),
                "category": sdk.Choice(instructions=spec["category"], criteria=CATEGORIES),
                **(
                    {"impact": sdk.Choice(instructions=spec["impact"], criteria=self._impact_rubric)}
                    if self._impact
                    else {}
                ),
                "novel": sdk.Noul(instructions=spec["novel"]),
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
        self._rebind()
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
            started = time.perf_counter()
            response = await self._get_client().system_one(state=state, questions=self._get_questions())
            elapsed_ms = round((time.perf_counter() - started) * 1000)
        return self._convert(response, requested_model=self._model, latency_ms=elapsed_ms)

    @staticmethod
    def _convert(response: Any, *, requested_model: str = DEFAULT_MODEL, latency_ms: int | None = None) -> ArticleScore:
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
        impact = answers.get("impact")
        impact_probs = {k: float(v) for k, v in ((impact.probabilities if impact else None) or {}).items()}
        expected_impact = getattr(impact, "choice", None) if impact else None
        if expected_impact not in IMPACT_LEVELS:
            expected_impact = None  # never invent a level the model did not give
        return ArticleScore(
            score=score,
            confidence=_unit(getattr(sentiment, "confidence", 1.0)),
            relevance=_unit(answers["relevant"].noul),
            expected_impact=expected_impact,
            labels={
                "impact_probs": impact_probs,
                "scorer": JevScorer.name,
                # What we asked for and what actually answered: with an alias these
                # differ, which is exactly why an alias is not cacheable.
                "requested_model": requested_model,
                "model": getattr(response, "model", None),
                "request_id": getattr(response, "request_id", None),
                "latency_ms": latency_ms,
                "usage": _usage(getattr(response, "usage", None)),
                "sentiment_probs": {int(k): float(v) for k, v in probs.items()},
                "category": category.choice,
                "category_probs": {k: float(v) for k, v in (category.probabilities or {}).items()},
                "novel": _unit(answers["novel"].noul),
            },
        )

    async def aclose(self) -> None:
        client, self._client = self._client, None
        self._loop = None
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


def _usage(usage: Any) -> dict[str, int]:
    """Tokens this call consumed. Recorded per article, so a later cache hit can be
    told apart from newly spent quota."""
    if usage is None:
        return {}
    out = {}
    for field in ("input_tokens", "output_tokens"):
        value = getattr(usage, field, None)
        if value is not None:
            out[field] = int(value)
    return out


def _unit(value: Any) -> float:
    return max(0.0, min(1.0, float(value)))
