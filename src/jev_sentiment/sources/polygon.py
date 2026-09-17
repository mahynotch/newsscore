"""Massive (formerly Polygon.io) ticker news. https://massive.com/docs/rest/stocks/news

Polygon.io rebranded to Massive in October 2025; keys and endpoints are unchanged
apart from the host, and ``api.polygon.io`` keeps working for now. The source is
registered under both ``polygon`` and ``massive``, and reads ``POLYGON_API_KEY``
or ``MASSIVE_API_KEY``. Option ``max_pages`` (default 5) bounds pagination.
"""

from __future__ import annotations

import os
from datetime import datetime

import httpx

from ..models import Article
from .base import NewsSource, in_window, parse_dt


class PolygonSource(NewsSource):
    type_name = "polygon"
    env_key = "POLYGON_API_KEY"
    URL = "https://api.massive.com/v2/reference/news"

    def __init__(self, api_key: str | None = None, name: str | None = None, **options: object) -> None:
        super().__init__(api_key or os.environ.get("MASSIVE_API_KEY"), name, **options)

    async def fetch(
        self, query: str, since: datetime, until: datetime, client: httpx.AsyncClient
    ) -> list[Article]:
        params: dict[str, object] | None = {
            "ticker": query.upper(),
            "published_utc.gte": since.isoformat(),
            "published_utc.lte": until.isoformat(),
            "order": "desc",
            "limit": 1000,
            "apiKey": self.api_key,
        }
        url = self.URL
        out: list[Article] = []
        for _ in range(int(self.options.get("max_pages", 5))):
            data = await self._get_json(client, url, params=params)
            for item in data.get("results") or []:
                published = parse_dt(item["published_utc"])
                if not in_window(published, since, until):
                    continue
                publisher = (item.get("publisher") or {}).get("name")
                out.append(
                    self._article(
                        title=item.get("title") or "",
                        published=published,
                        url=item.get("article_url"),
                        summary=item.get("description"),
                        symbols=item.get("tickers") or [query],
                        raw={**item, "publisher_name": publisher},
                    )
                )
            next_url = data.get("next_url")
            if not next_url:
                break
            # httpx replaces a URL's query string when params= is given, so merge the key in by hand.
            url = str(httpx.URL(next_url).copy_merge_params({"apiKey": self.api_key or ""}))
            params = None
        return out


class MassiveSource(PolygonSource):
    """Same provider under its new name."""

    type_name = "massive"
    env_key = "MASSIVE_API_KEY"
