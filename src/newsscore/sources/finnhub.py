"""Finnhub company news. https://finnhub.io/docs/api/company-news"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # only annotations need httpx here; fetching imports it for real
    import httpx

from ..models import Article
from .base import NewsSource, in_window, parse_dt


class FinnhubSource(NewsSource):
    type_name = "finnhub"
    env_key = "FINNHUB_API_KEY"
    URL = "https://finnhub.io/api/v1/company-news"

    async def fetch(
        self, query: str, since: datetime, until: datetime, client: httpx.AsyncClient
    ) -> list[Article]:
        params = {
            "symbol": query.upper(),
            "from": since.date().isoformat(),
            "to": until.date().isoformat(),
            "token": self.api_key,
        }
        items = await self._get_json(client, self.URL, params=params)
        out: list[Article] = []
        for item in items or []:
            published = parse_dt(item["datetime"])
            if not in_window(published, since, until):
                continue
            out.append(
                self._article(
                    title=item.get("headline") or "",
                    published=published,
                    url=item.get("url"),
                    summary=item.get("summary"),
                    symbols=[item.get("related") or query],
                    raw=item,
                )
            )
        return out
