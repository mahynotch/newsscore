"""A dependency-free lexicon scorer.

Inspired by the Loughran-McDonald finance word lists but far smaller. It exists so
the library works with zero API keys (tests, smoke checks, offline fallback), not
because it is a good trading signal. Use :class:`~newsscore.scoring.jev.JevScorer`
or your own model for real work.

Method
    score      = (positive_hits - negative_hits) / (positive_hits + negative_hits + 1)
    confidence = 1 - exp(-hits / 3)
    relevance  = 1.0 if the query appears in the text or symbols, else 0.5
A negator ("not", "no", "never", "without", "fails to") within two tokens before a
sentiment word flips its sign.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Sequence

from ..models import Article, ArticleScore

POSITIVE = frozenset(
    """
    beat beats beating exceed exceeds exceeded surpass surpasses surpassed record
    strong stronger strongest robust growth grow grows grew accelerate accelerates
    upgrade upgrades upgraded outperform outperforms outperformed raise raises raised
    boost boosts boosted gain gains gained rally rallies rallied surge surges surged
    soar soars soared jump jumps jumped profit profits profitable improve improves
    improved improvement optimistic bullish momentum breakthrough approval approved
    win wins won award awarded contract expansion expand expands innovative
    dividend buyback upbeat positive success successful benefit benefits favorable
    resilient recover recovers recovered recovery exceeding milestone
    """.split()
)

NEGATIVE = frozenset(
    """
    miss misses missed shortfall weak weaker weakest decline declines declined drop
    drops dropped fall falls fell plunge plunges plunged slump slumps slumped tumble
    tumbles tumbled crash crashes crashed loss losses lose loses lost downgrade
    downgrades downgraded underperform underperforms underperformed cut cuts warning
    warns warned lawsuit sued sue litigation probe investigation investigate fraud
    recall recalls recalled bankruptcy bankrupt default defaults delay delays delayed
    layoff layoffs fire fired resign resigns resigned scandal fine fined penalty
    risk risks risky concern concerns worried bearish pessimistic negative disappoint
    disappoints disappointed disappointing halt halted suspend suspended volatile
    volatility uncertainty uncertain headwind headwinds pressure slowdown slowing
    weakness impairment writedown write-down guidance-cut breach outage
    """.split()
)

NEGATORS = frozenset("not no never without hardly barely fails failed".split())
_TOKEN = re.compile(r"[a-z][a-z'\-]*")


def _score_text(text: str) -> tuple[float, int]:
    tokens = _TOKEN.findall(text.lower())
    pos = neg = 0
    for i, tok in enumerate(tokens):
        polarity = 1 if tok in POSITIVE else -1 if tok in NEGATIVE else 0
        if polarity == 0:
            continue
        if any(t in NEGATORS for t in tokens[max(0, i - 2) : i]):
            polarity = -polarity
        if polarity > 0:
            pos += 1
        else:
            neg += 1
    hits = pos + neg
    return (pos - neg) / (hits + 1), hits


class KeywordScorer:
    """Lexicon-based :data:`~newsscore.scoring.protocol.ScoreFn`. See module docs."""

    name = "keyword-v1"

    @property
    def fingerprint(self) -> str:
        """Digest of the lexicon, so editing a word list invalidates old scores."""
        blob = "|".join(sorted(POSITIVE) + sorted(NEGATIVE) + sorted(NEGATORS))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

    def __call__(self, articles: Sequence[Article], query: str) -> list[ArticleScore]:
        q = query.lower()
        out: list[ArticleScore] = []
        for article in articles:
            score, hits = _score_text(article.text)
            mentioned = q in article.text.lower() or any(q == s.lower() for s in article.symbols)
            out.append(
                ArticleScore(
                    score=score,
                    confidence=1.0 - math.exp(-hits / 3.0),
                    relevance=1.0 if mentioned else 0.5,
                    labels={"hits": hits, "scorer": self.name},
                )
            )
        return out
