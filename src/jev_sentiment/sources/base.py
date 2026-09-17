"""Base class and helpers for news sources.

To add a provider, subclass :class:`NewsSource`, set ``type_name`` (and ``env_key``
if it needs a key), implement :meth:`NewsSource.fetch`, and register the class in
``sources/__init__.py``. Fetch implementations should use :meth:`_get_json` and
:meth:`_article` so errors and ids behave the same across providers.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, ClassVar, Iterable, Mapping

import httpx

from ..models import UTC, Article, make_id


class SourceError(RuntimeError):
    """A provider call failed. Carries the source name and, when known, the HTTP status."""

    def __init__(self, source: str, message: str, status: int | None = None) -> None:
        self.source = source
        self.status = status
        prefix = f"{source}: " if source else ""
        suffix = f" (HTTP {status})" if status else ""
        super().__init__(f"{prefix}{message}{suffix}")


class NewsSource(ABC):
    """A provider of :class:`~jev_sentiment.Article` objects.

    Class attributes:
        type_name: Registry key, used by the CLI (``jevsent source add <type>``).
        env_key: Environment variable consulted when no ``api_key`` is passed.
        requires_key: Set ``False`` for keyless providers such as RSS.
        query_kind: ``"symbol"`` if the provider expects a ticker, ``"keyword"``
            if it does free-text search. Informational; the engine passes the
            query through unchanged either way.
    """

    type_name: ClassVar[str]
    env_key: ClassVar[str | None] = None
    requires_key: ClassVar[bool] = True
    query_kind: ClassVar[str] = "symbol"

    def __init__(self, api_key: str | None = None, name: str | None = None, **options: Any) -> None:
        self.name = name or self.type_name
        self.api_key = api_key or (os.environ.get(self.env_key) if self.env_key else None)
        self.options = options
        if self.requires_key and not self.api_key:
            hint = f"pass api_key= or set {self.env_key}" if self.env_key else "pass api_key="
            raise SourceError(self.name, f"API key required; {hint}")

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"<{type(self).__name__} name={self.name!r}>"

    @abstractmethod
    async def fetch(
        self, query: str, since: datetime, until: datetime, client: httpx.AsyncClient
    ) -> list[Article]:
        """Return every article about ``query`` published in ``[since, until]``.

        ``since`` and ``until`` are timezone-aware UTC. Implementations should
        filter with :func:`in_window` because many providers only accept dates.
        """

    # ---- helpers for subclasses -------------------------------------------------

    async def _get_json(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        try:
            response = await client.get(url, params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise SourceError(self.name, f"request failed: {exc}") from exc
        if response.status_code >= 400:
            raise SourceError(self.name, _short(response.text), response.status_code)
        try:
            return response.json()
        except ValueError as exc:
            raise SourceError(self.name, f"invalid JSON: {_short(response.text)}") from exc

    def _article(
        self,
        *,
        title: str,
        published: datetime,
        url: str | None = None,
        summary: str | None = None,
        symbols: Iterable[str] = (),
        raw: Mapping[str, Any] | None = None,
    ) -> Article:
        key = url or f"{title}|{published.isoformat()}"
        return Article(
            id=make_id(self.name, key),
            source=self.name,
            title=title.strip(),
            published=published,
            url=url,
            summary=(summary or "").strip() or None,
            symbols=tuple(s.upper() for s in symbols if s),
            raw=dict(raw or {}),
        )


# ---- date helpers ---------------------------------------------------------------


def to_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def parse_dt(value: Any) -> datetime:
    """Parse the timestamp formats seen across news APIs into aware UTC.

    Handles unix seconds/milliseconds, ISO 8601 (with ``Z``), Alpha Vantage's
    ``YYYYMMDDTHHMMSS`` and RFC 2822 (RSS ``pubDate``).
    """
    if isinstance(value, datetime):
        return to_utc(value)
    if isinstance(value, (int, float)):
        seconds = value / 1000.0 if value > 1e11 else value
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    text = str(value).strip()
    if text.isdigit():
        return parse_dt(int(text))
    if len(text) == 15 and text[8] == "T" and text[:8].isdigit():
        return datetime.strptime(text, "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
    try:
        return to_utc(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        pass
    try:
        return to_utc(parsedate_to_datetime(text))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"unrecognised timestamp: {value!r}") from exc


def in_window(published: datetime, since: datetime, until: datetime) -> bool:
    return since <= published <= until


def _short(text: str, limit: int = 200) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."
