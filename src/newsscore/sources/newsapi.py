"""NewsAPI.org "everything" search. https://newsapi.org/docs/endpoints/everything

This is a keyword search, so pass a company name (``"Apple"``) rather than a
ticker for best results. Options: ``language`` (default ``en``), ``max_pages``
(default 1; the free plan allows only the first 100 results).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # only annotations need httpx here; fetching imports it for real
    import httpx

from ..models import Article
from .base import NewsSource, SourceError, in_window, parse_dt


class NewsApiSource(NewsSource):
    type_name = "newsapi"
    env_key = "NEWSAPI_API_KEY"
    query_kind = "keyword"
    URL = "https://newsapi.org/v2/everything"

    async def fetch(
        self, query: str, since: datetime, until: datetime, client: httpx.AsyncClient
    ) -> list[Article]:
        out: list[Article] = []
        for page in range(1, int(self.options.get("max_pages", 1)) + 1):
            params = {
                "q": query,
                "from": since.isoformat(timespec="seconds"),
                "to": until.isoformat(timespec="seconds"),
                "language": self.options.get("language", "en"),
                "sortBy": "publishedAt",
                "pageSize": 100,
                "page": page,
            }
            data = await self._get_json(
                client, self.URL, params=params, headers={"X-Api-Key": self.api_key or ""}
            )
            if data.get("status") != "ok":
                raise SourceError(self.name, data.get("message") or "unknown error", secret=self.api_key)
            items = data.get("articles") or []
            for item in items:
                published = parse_dt(item["publishedAt"])
                if not in_window(published, since, until):
                    continue
                out.append(
                    self._article(
                        title=item.get("title") or "",
                        published=published,
                        url=item.get("url"),
                        summary=item.get("description"),
                        raw={**item, "publisher_name": (item.get("source") or {}).get("name")},
                    )
                )
            if len(items) < 100:
                break
        return out
