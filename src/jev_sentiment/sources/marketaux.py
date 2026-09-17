"""Marketaux news. https://www.marketaux.com/documentation

Option ``max_pages`` (default 3) bounds pagination; free plans return 3 items per page.
"""

from __future__ import annotations

from datetime import datetime

import httpx

from ..models import Article
from .base import NewsSource, in_window, parse_dt


class MarketauxSource(NewsSource):
    type_name = "marketaux"
    env_key = "MARKETAUX_API_KEY"
    URL = "https://api.marketaux.com/v1/news/all"

    async def fetch(
        self, query: str, since: datetime, until: datetime, client: httpx.AsyncClient
    ) -> list[Article]:
        out: list[Article] = []
        for page in range(1, int(self.options.get("max_pages", 3)) + 1):
            params = {
                "symbols": query.upper(),
                "published_after": since.strftime("%Y-%m-%dT%H:%M"),
                "published_before": until.strftime("%Y-%m-%dT%H:%M"),
                "language": self.options.get("language", "en"),
                "page": page,
                "api_token": self.api_key,
            }
            data = await self._get_json(client, self.URL, params=params)
            items = data.get("data") or []
            for item in items:
                published = parse_dt(item["published_at"])
                if not in_window(published, since, until):
                    continue
                symbols = [e.get("symbol", "") for e in item.get("entities") or []]
                out.append(
                    self._article(
                        title=item.get("title") or "",
                        published=published,
                        url=item.get("url"),
                        summary=item.get("description") or item.get("snippet"),
                        symbols=symbols or [query],
                        raw=item,
                    )
                )
            meta = data.get("meta") or {}
            if not items or meta.get("returned", 0) < meta.get("limit", 1):
                break
        return out
