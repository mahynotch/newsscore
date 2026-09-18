"""newsscore: news sentiment scoring for stocks with a pluggable scorer.

    from newsscore import NewsScorer

    scorer = NewsScorer()                 # Jev if TYPESAFE_API_KEY is set, else keyword scorer
    scorer.source_add("yahoo")            # keyless RSS, good for a first try
    print(scorer.score("AAPL").score)

See ``newsscore.scoring.protocol`` for how to plug in your own scoring function.
"""

from ._version import __version__
from .aggregate import (
    IMPACT_WEIGHTS,
    Aggregate,
    AggregateFn,
    Contribution,
    aggregate,
    make_aggregator,
    quant_aggregator,
)
from .cache import ScoreCache
from .config import load_env
from .models import IMPACT_LEVELS, Article, ArticleScore, RunCounts, RunStatus, ScoreFailure, ScoredArticle, ScoreResult
from .scorer import NewsScorer
from .scoring import JevScorer, KeywordScorer, ScoreFn, ScoreItemError, ScorerUnavailable, per_article
from .sources import SOURCE_TYPES, NewsSource, SourceError, register

__all__ = [
    "__version__",
    "NewsScorer",
    "Article",
    "ArticleScore",
    "ScoredArticle",
    "ScoreResult",
    "ScoreFailure",
    "RunStatus",
    "RunCounts",
    "ScoreFn",
    "ScoreItemError",
    "ScorerUnavailable",
    "per_article",
    "JevScorer",
    "KeywordScorer",
    "NewsSource",
    "SourceError",
    "SOURCE_TYPES",
    "register",
    "Aggregate",
    "Contribution",
    "quant_aggregator",
    "IMPACT_LEVELS",
    "IMPACT_WEIGHTS",
    "AggregateFn",
    "aggregate",
    "make_aggregator",
    "ScoreCache",
    "load_env",
]
