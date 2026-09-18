"""Turn many per-article scores into one number.

The default scheme is deliberately simple and fully described here so it can be
reasoned about, back-tested and replaced:

    weight_i   = confidence_i * relevance_i * impact_i * 0.5 ** (age_hours_i / half_life_hours)
    score      = sum(weight_i * score_i) / sum(weight_i)
    confidence = 1 - exp(-sum(weight_i) / saturation)

``impact_i`` is 1.0 unless impact weighting is switched on, so by default this is
exactly the confidence/relevance/time-decay scheme and nothing else.

Every run also reports, per article, the score and each weight term that produced it,
plus a normalised ``contribution``. Those contributions sum to the reported score, so
an aggregate can always be traced back to the articles that moved it.

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
from typing import Any, Callable, Mapping, Sequence

from .models import IMPACT_LEVELS, ScoredArticle

#: The impact mapping used by the existing quant aggregator. Not applied unless you
#: ask for it: pass ``impact_weights=IMPACT_WEIGHTS`` (or your own mapping) to
#: :class:`~newsscore.NewsScorer` or :func:`make_aggregator`.
IMPACT_WEIGHTS: dict[str, float] = {"high": 3.0, "medium": 1.5, "low": 1.0}


@dataclass(frozen=True, slots=True)
class Contribution:
    """Every factor that went into one article's share of the aggregate.

    ``contribution`` is the article's signed, normalised share: the contributions of
    a run sum to its reported ``score``. The rest are the factors that produced it,
    so a surprising aggregate can be traced to the article and the term responsible.
    """

    article_id: str
    source: str
    score: float
    confidence: float
    relevance: float
    impact: float
    decay: float
    weight: float
    contribution: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "article_id": self.article_id,
            "source": self.source,
            "score": self.score,
            "confidence": self.confidence,
            "relevance": self.relevance,
            "impact": self.impact,
            "decay": self.decay,
            "weight": self.weight,
            "contribution": self.contribution,
        }


@dataclass(frozen=True, slots=True)
class Aggregate:
    score: float
    confidence: float
    by_source: dict[str, float] = field(default_factory=dict)
    #: Sum of the final weights. ``None`` from a custom aggregator that does not
    #: report one, which is how the engine tells "no usable weight" apart from
    #: "this aggregator does not measure weight".
    total_weight: float | None = None
    #: Per-article breakdown, when the aggregator can produce one. Empty for a
    #: custom ``aggregate_fn``: contributions summing to the score is a property of
    #: a weighted mean, not of aggregation in general.
    contributions: list[Contribution] = field(default_factory=list)


AggregateFn = Callable[[Sequence[ScoredArticle], datetime], Aggregate]


def aggregate(
    scored: Sequence[ScoredArticle],
    now: datetime,
    *,
    half_life_hours: float = 48.0,
    saturation: float = 3.0,
    impact_weights: Mapping[str, float] | None = None,
    use_relevance: bool = True,
    lookback_hours: float | None = None,
) -> Aggregate:
    """Weighted mean with exponential time decay, and a full audit trail.

    Args:
        scored: The articles to combine.
        now: Reference time. Anything published after it is excluded rather than
            treated as the freshest evidence available.
        half_life_hours: Decay half-life. ``0`` disables decay.
        saturation: How fast ``confidence`` approaches 1 in total weight.
        impact_weights: Off by default. Pass a mapping such as :data:`IMPACT_WEIGHTS`
            to weight by ``ArticleScore.expected_impact``. Articles whose scorer
            supplies no impact keep ``1.0``, so a scorer that cannot judge impact
            (the keyword scorer, your own function) is unaffected.
        use_relevance: Set ``False`` to drop relevance from the weight entirely, for
            pipelines that filter by relevance themselves.
        lookback_hours: Ignore articles older than this. ``None`` keeps them all,
            letting decay do the work. Separate from the *fetch* window: this bounds
            what is aggregated, not what is collected.

    Returns:
        An :class:`Aggregate` carrying the score, the evidence confidence, the total
        weight and one :class:`Contribution` per article that counted.
    """
    if impact_weights is not None:
        unknown = set(impact_weights) - set(IMPACT_LEVELS)
        if unknown:
            raise ValueError(f"impact_weights keys must be in {IMPACT_LEVELS}, got {sorted(unknown)}")
    if not scored:
        return Aggregate(score=0.0, confidence=0.0, total_weight=0.0)

    rows: list[tuple[ScoredArticle, float, float, float, float]] = []
    for item in scored:
        age_hours = (now - item.article.published).total_seconds() / 3600.0
        if age_hours < 0.0:
            continue  # published after `now`: not evidence as of this reference time
        if lookback_hours is not None and age_hours > lookback_hours:
            continue
        decay = 0.5 ** (age_hours / half_life_hours) if half_life_hours > 0 else 1.0
        relevance = item.score.relevance if use_relevance else 1.0
        impact = 1.0
        if impact_weights is not None and item.score.expected_impact is not None:
            impact = float(impact_weights.get(item.score.expected_impact, 1.0))
        weight = item.score.confidence * relevance * impact * decay
        if weight <= 0.0:
            continue
        rows.append((item, relevance, impact, decay, weight))

    total_w = math.fsum(weight for *_, weight in rows)
    if total_w == 0.0:
        return Aggregate(score=0.0, confidence=0.0, total_weight=0.0)

    total_ws = math.fsum(weight * item.score.score for item, _, _, _, weight in rows)
    per_source_w: dict[str, float] = defaultdict(float)
    per_source_ws: dict[str, float] = defaultdict(float)
    contributions: list[Contribution] = []
    for item, relevance, impact, decay, weight in rows:
        per_source_w[item.article.source] += weight
        per_source_ws[item.article.source] += weight * item.score.score
        contributions.append(
            Contribution(
                article_id=item.article.id,
                source=item.article.source,
                score=item.score.score,
                confidence=item.score.confidence,
                relevance=relevance,
                impact=impact,
                decay=decay,
                weight=weight,
                contribution=weight * item.score.score / total_w,
            )
        )

    by_source = {name: per_source_ws[name] / per_source_w[name] for name in per_source_w}
    confidence = 1.0 - math.exp(-total_w / saturation) if saturation > 0 else 1.0
    return Aggregate(
        score=total_ws / total_w,
        confidence=confidence,
        by_source=by_source,
        total_weight=total_w,
        contributions=contributions,
    )


def make_aggregator(
    half_life_hours: float = 48.0,
    saturation: float = 3.0,
    impact_weights: Mapping[str, float] | None = None,
    use_relevance: bool = True,
    lookback_hours: float | None = None,
) -> AggregateFn:
    """Bind parameters so the result matches the :data:`AggregateFn` signature."""
    return partial(
        aggregate,
        half_life_hours=half_life_hours,
        saturation=saturation,
        impact_weights=impact_weights,
        use_relevance=use_relevance,
        lookback_hours=lookback_hours,
    )


def quant_aggregator() -> AggregateFn:
    """The scheme the existing quant aggregator uses, as a drop-in ``aggregate_fn``.

    Confidence times impact times decay, a 12-hour half-life, a 48-hour lookback and
    no relevance term. Provided so that pipeline can be reproduced exactly without
    changing this library's own defaults, which stay general-purpose::

        NewsScorer(aggregate_fn=quant_aggregator())

    Compare it against the default on the same saved scores with ``aggregate_scored``;
    neither needs a model call.
    """
    return make_aggregator(
        half_life_hours=12.0,
        lookback_hours=48.0,
        use_relevance=False,
        impact_weights=IMPACT_WEIGHTS,
    )
