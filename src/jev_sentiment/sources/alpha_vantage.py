"""Alpha Vantage NEWS_SENTIMENT. https://www.alphavantage.co/documentation/#news-sentiment

The vendor's own sentiment fields are kept in ``Article.raw`` (``overall_sentiment_score``,
``ticker_sentiment``) for anyone who wants to build a scorer on top of them.
"""

from __future__ import annotations

from datetime import datetime

import httpx

from ..models import Article
from .base import NewsSource, SourceError, in_window, parse_dt


class AlphaVantageSource(NewsSource):
    type_name = "alpha_vantage"
    env_key = "ALPHAVANTAGE_API_KEY"
    URL = "https://www.alphavantage.co/query"

    async def fetch(
        self, query: str, since: datetime, until: datetime, client: httpx.AsyncClient
    ) -> list[Article]:
        params = {
            "function": "NEWS_SENTIMENT",
            "tickers": query.upper(),
            "time_from": since.strftime("%Y%m%dT%H%M"),
            "time_to": until.strftime("%Y%m%dT%H%M"),
            "sort": "LATEST",
            "limit": int(self.options.get("limit", 1000)),
            "apikey": self.api_key,
        }
        data = await self._get_json(client, self.URL, params=params)
        # Alpha Vantage reports quota and key problems as 200 OK with a message.
        for key in ("Information", "Note", "Error Message"):
            if key in data:
                raise SourceError(self.name, str(data[key]))
        out: list[Article] = []
        for item in data.get("feed") or []:
            published = parse_dt(item["time_published"])
            if not in_window(published, since, until):
                continue
            symbols = [t.get("ticker", "") for t in item.get("ticker_sentiment") or []]
            out.append(
                self._article(
                    title=item.get("title") or "",
                    published=published,
                    url=item.get("url"),
                    summary=item.get("summary"),
                    symbols=symbols or [query],
                    raw=item,
                )
            )
        return out
