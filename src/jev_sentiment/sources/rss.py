"""Generic RSS 2.0 / Atom feeds, plus a Yahoo Finance preset. No API key needed.

Options:
    url: Feed URL. ``{query}`` inside it is replaced by the query, so one saved
        source can serve every ticker, e.g.
        ``https://feeds.finance.yahoo.com/rss/2.0/headline?s={query}``.
    match: If ``True`` (default) keep only items whose title or summary mentions
        the query, case-insensitively. Set ``False`` for per-ticker feeds where
        every item is already relevant (the Yahoo preset does this).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Iterator

import httpx

from ..models import Article
from .base import NewsSource, SourceError, in_window, parse_dt

_TAG_RE = re.compile(r"<[^>]+>")
_ATOM = "{http://www.w3.org/2005/Atom}"


def _text(el: ET.Element | None) -> str:
    return _TAG_RE.sub("", (el.text or "")).strip() if el is not None else ""


class RssSource(NewsSource):
    type_name = "rss"
    requires_key = False
    query_kind = "keyword"
    default_url: str | None = None
    default_match = True

    def __init__(self, api_key: str | None = None, name: str | None = None, **options: object) -> None:
        super().__init__(api_key, name, **options)
        self.url = str(self.options.get("url") or self.default_url or "")
        if not self.url:
            raise SourceError(self.name, "an RSS source needs option url=...")
        self.match = bool(self.options.get("match", self.default_match))

    async def fetch(
        self, query: str, since: datetime, until: datetime, client: httpx.AsyncClient
    ) -> list[Article]:
        url = self.url.replace("{query}", query)
        try:
            response = await client.get(url, headers={"Accept": "application/rss+xml, application/xml, text/xml, */*"})
        except httpx.HTTPError as exc:
            raise SourceError(self.name, f"request failed: {exc}") from exc
        if response.status_code >= 400:
            raise SourceError(self.name, "feed request failed", response.status_code)
        try:
            root = ET.fromstring(response.content)
        except ET.ParseError as exc:
            raise SourceError(self.name, f"invalid feed XML: {exc}") from exc

        needle = query.lower()
        out: list[Article] = []
        for title, link, summary, stamp in _entries(root):
            if not title or not stamp:
                continue
            try:
                published = parse_dt(stamp)
            except ValueError:
                continue
            if not in_window(published, since, until):
                continue
            if self.match and needle not in f"{title} {summary}".lower():
                continue
            out.append(self._article(title=title, published=published, url=link or None, summary=summary))
        return out


def _entries(root: ET.Element) -> Iterator[tuple[str, str, str, str]]:
    """Yield (title, link, summary, timestamp) for RSS ``item`` and Atom ``entry`` nodes."""
    for item in root.iter("item"):  # RSS 2.0
        yield (
            _text(item.find("title")),
            _text(item.find("link")),
            _text(item.find("description")),
            _text(item.find("pubDate")) or _text(item.find("{http://purl.org/dc/elements/1.1/}date")),
        )
    for entry in root.iter(f"{_ATOM}entry"):  # Atom
        link_el = entry.find(f"{_ATOM}link")
        link = link_el.get("href", "") if link_el is not None else ""
        yield (
            _text(entry.find(f"{_ATOM}title")),
            link,
            _text(entry.find(f"{_ATOM}summary")) or _text(entry.find(f"{_ATOM}content")),
            _text(entry.find(f"{_ATOM}published")) or _text(entry.find(f"{_ATOM}updated")),
        )


class YahooSource(RssSource):
    """Yahoo Finance per-ticker headline feed. Keyless; good for a first smoke test."""

    type_name = "yahoo"
    default_url = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={query}&region=US&lang=en-US"
    default_match = False
