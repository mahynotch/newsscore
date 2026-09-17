"""Tiingo news. https://www.tiingo.com/documentation/news"""

from __future__ import annotations

from datetime import datetime

import httpx

from ..models import Article
from .base import NewsSource, in_window, parse_dt


class TiingoSource(NewsSource):
    type_name = "tiingo"
    env_key = "TIINGO_API_KEY"
    URL = "https://api.tiingo.com/tiingo/news"

    async def fetch(
        self, query: str, since: datetime, until: datetime, client: httpx.AsyncClient
    ) -> list[Article]:
        params = {
            "tickers": query.lower(),
            "startDate": since.date().isoformat(),
            "endDate": until.date().isoformat(),
            "limit": int(self.options.get("limit", 1000)),
            "sortBy": "publishedDate",
            "token": self.api_key,
        }
        items = await self._get_json(client, self.URL, params=params)
        out: list[Article] = []
        for item in items or []:
            published = parse_dt(item["publishedDate"])
            if not in_window(published, since, until):
                continue
            out.append(
                self._article(
                    title=item.get("title") or "",
                    published=published,
                    url=item.get("url"),
                    summary=item.get("description"),
                    symbols=item.get("tickers") or [query],
                    raw=item,
                )
            )
        return out
