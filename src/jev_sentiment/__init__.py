"""jev_sentiment: news sentiment scoring for stocks with a pluggable scorer.

    from jev_sentiment import NewsScorer

    scorer = NewsScorer()                 # Jev if TYPESAFE_API_KEY is set, else keyword scorer
    scorer.source_add("yahoo")            # keyless RSS, good for a first try
    print(scorer.score("AAPL").score)

See ``jev_sentiment.scoring.protocol`` for how to plug in your own scoring function.
"""

from ._version import __version__
from .aggregate import Aggregate, AggregateFn, aggregate, make_aggregator
from .cache import ScoreCache
from .config import load_env
from .models import Article, ArticleScore, ScoredArticle, ScoreResult
from .scorer import NewsScorer
from .scoring import JevScorer, KeywordScorer, ScoreFn, per_article
from .sources import SOURCE_TYPES, NewsSource, SourceError, register

__all__ = [
    "__version__",
    "NewsScorer",
    "Article",
    "ArticleScore",
    "ScoredArticle",
    "ScoreResult",
    "ScoreFn",
    "per_article",
    "JevScorer",
    "KeywordScorer",
    "NewsSource",
    "SourceError",
    "SOURCE_TYPES",
    "register",
    "Aggregate",
    "AggregateFn",
    "aggregate",
    "make_aggregator",
    "ScoreCache",
    "load_env",
]
