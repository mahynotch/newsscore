"""Turn many per-article scores into one number.

The default scheme is deliberately simple and fully described here so it can be
reasoned about, back-tested and replaced:

    weight_i   = confidence_i * relevance_i * impact_i * 0.5 ** (age_hours_i / half_life_hours)
    score      = sum(weight_i * score_i) / sum(weight_i)
    confidence = 1 - exp(-sum(weight_i) / saturation)

``impact_i`` is 1.0 unless impact weighting is switched on, so by default this is
exactly the confidence/relevance/time-decay scheme and nothing else.

Older articles count less (exponential decay), unsure or off-topic articles count
less (their weights), and confidence grows with the amount of weighted evidence
but never exceeds 1. With ``saturation=3`` three fully-weighted fresh articles
give confidence ~0.63; ten give ~0.96.

Plug in your own with ``NewsScorer(aggregate_fn=...)``; it receives the scored
articles and the reference time and must return an :class:`Aggregate`.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from typing import Callable, Mapping, Sequence

from .models import IMPACT_LEVELS, ScoredArticle

#: The impact mapping used by the existing quant aggregator. Not applied unless you
#: ask for it: pass ``impact_weights=IMPACT_WEIGHTS`` (or your own mapping) to
#: :class:`~newsscore.NewsScorer` or :func:`make_aggregator`.
IMPACT_WEIGHTS: dict[str, float] = {"high": 3.0, "medium": 1.5, "low": 1.0}


@dataclass(frozen=True, slots=True)
class Aggregate:
    score: float
    confidence: float
    by_source: dict[str, float] = field(default_factory=dict)


AggregateFn = Callable[[Sequence[ScoredArticle], datetime], Aggregate]


def aggregate(
    scored: Sequence[ScoredArticle],
    now: datetime,
    *,
    half_life_hours: float = 48.0,
    saturation: float = 3.0,
    impact_weights: Mapping[str, float] | None = None,
) -> Aggregate:
    """Confidence- and relevance-weighted mean with exponential time decay.

    ``impact_weights`` is off by default. Pass a mapping such as
    :data:`IMPACT_WEIGHTS` to also weight by ``ArticleScore.expected_impact``.
    Articles whose scorer supplies no impact keep weight ``1.0``, so turning this on
    changes only the articles that actually carry a judgement, and a scorer that
    cannot produce one (the keyword scorer, your own function) keeps working.
    """
    if impact_weights is not None:
        unknown = set(impact_weights) - set(IMPACT_LEVELS)
        if unknown:
            raise ValueError(f"impact_weights keys must be in {IMPACT_LEVELS}, got {sorted(unknown)}")
    if not scored:
        return Aggregate(score=0.0, confidence=0.0)

    total_w = 0.0
    total_ws = 0.0
    per_source_w: dict[str, float] = defaultdict(float)
    per_source_ws: dict[str, float] = defaultdict(float)

    for item in scored:
        age_hours = max(0.0, (now - item.article.published).total_seconds() / 3600.0)
        decay = 0.5 ** (age_hours / half_life_hours) if half_life_hours > 0 else 1.0
        impact = 1.0
        if impact_weights is not None and item.score.expected_impact is not None:
            impact = float(impact_weights.get(item.score.expected_impact, 1.0))
        w = item.score.weight * decay * impact
        if w <= 0.0:
            continue
        total_w += w
        total_ws += w * item.score.score
        per_source_w[item.article.source] += w
        per_source_ws[item.article.source] += w * item.score.score

    if total_w == 0.0:
        return Aggregate(score=0.0, confidence=0.0)

    by_source = {name: per_source_ws[name] / per_source_w[name] for name in per_source_w}
    confidence = 1.0 - math.exp(-total_w / saturation) if saturation > 0 else 1.0
    return Aggregate(score=total_ws / total_w, confidence=confidence, by_source=by_source)


def make_aggregator(
    half_life_hours: float = 48.0,
    saturation: float = 3.0,
    impact_weights: Mapping[str, float] | None = None,
) -> AggregateFn:
    """Bind parameters so the result matches the :data:`AggregateFn` signature."""
    return partial(
        aggregate,
        half_life_hours=half_life_hours,
        saturation=saturation,
        impact_weights=impact_weights,
    )
