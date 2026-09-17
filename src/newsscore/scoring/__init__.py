"""Built-in scorers and the scoring-function contract."""

from __future__ import annotations

import importlib.util
import logging
import os
from typing import Any, Callable

from .jev import JevScorer
from .keyword import KeywordScorer
from .protocol import ScoreFn, ScoreItem, ScoreOutput, call_score_fn, normalise_scores, per_article, scorer_name

log = logging.getLogger(__name__)

SCORERS: dict[str, Callable[..., ScoreFn]] = {
    "jev": JevScorer,
    "keyword": KeywordScorer,
}


def make_scorer(name: str, **kwargs: Any) -> ScoreFn:
    try:
        factory = SCORERS[name]
    except KeyError:
        raise ValueError(f"unknown scorer {name!r}; known: {', '.join(sorted(SCORERS))}") from None
    return factory(**kwargs)


def jev_available() -> bool:
    return bool(os.environ.get("TYPESAFE_API_KEY")) and importlib.util.find_spec("typesafe_sdk") is not None


def default_scorer() -> ScoreFn:
    """Jev when it can run (SDK installed and ``TYPESAFE_API_KEY`` set), else the keyword scorer."""
    if jev_available():
        return JevScorer()
    log.info("TYPESAFE_API_KEY or typesafe-sdk missing; falling back to the keyword scorer")
    return KeywordScorer()


__all__ = [
    "SCORERS",
    "ScoreFn",
    "ScoreItem",
    "ScoreOutput",
    "JevScorer",
    "KeywordScorer",
    "call_score_fn",
    "default_scorer",
    "jev_available",
    "make_scorer",
    "normalise_scores",
    "per_article",
    "scorer_name",
]
