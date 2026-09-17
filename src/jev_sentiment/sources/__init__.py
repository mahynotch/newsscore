"""Registry of built-in news sources.

``SOURCE_TYPES`` maps the CLI type name to the class. Third-party code can add
its own with :func:`register`.
"""

from __future__ import annotations

from typing import Any

from .alpha_vantage import AlphaVantageSource
from .base import NewsSource, SourceError, in_window, parse_dt
from .finnhub import FinnhubSource
from .marketaux import MarketauxSource
from .newsapi import NewsApiSource
from .polygon import MassiveSource, PolygonSource
from .rss import RssSource, YahooSource
from .tiingo import TiingoSource

SOURCE_TYPES: dict[str, type[NewsSource]] = {
    cls.type_name: cls
    for cls in (
        FinnhubSource,
        AlphaVantageSource,
        PolygonSource,
        MassiveSource,
        TiingoSource,
        MarketauxSource,
        NewsApiSource,
        RssSource,
        YahooSource,
    )
}


def register(cls: type[NewsSource]) -> type[NewsSource]:
    """Class decorator: make a custom source available by ``type_name``."""
    SOURCE_TYPES[cls.type_name] = cls
    return cls


def make_source(type_name: str, *, api_key: str | None = None, name: str | None = None, **options: Any) -> NewsSource:
    try:
        cls = SOURCE_TYPES[type_name]
    except KeyError:
        known = ", ".join(sorted(SOURCE_TYPES))
        raise SourceError(type_name, f"unknown source type; known types: {known}") from None
    return cls(api_key=api_key, name=name, **options)


__all__ = [
    "SOURCE_TYPES",
    "NewsSource",
    "SourceError",
    "make_source",
    "register",
    "in_window",
    "parse_dt",
    "FinnhubSource",
    "AlphaVantageSource",
    "PolygonSource",
    "MassiveSource",
    "TiingoSource",
    "MarketauxSource",
    "NewsApiSource",
    "RssSource",
    "YahooSource",
]
