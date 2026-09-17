"""Each provider parser against a fixture payload, mocked with respx."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import httpx
import pytest
import respx

from newsscore.sources import SourceError, make_source
from newsscore.sources.base import parse_dt

from conftest import NOW

SINCE, UNTIL = NOW - timedelta(days=7), NOW
IN = "2026-09-17T10:00:00Z"
OUT = "2026-01-01T10:00:00Z"


def run(source, query="AAPL"):
    async def go():
        async with httpx.AsyncClient() as client:
            return await source.fetch(query, SINCE, UNTIL, client)

    return asyncio.run(go())


def test_parse_dt_formats():
    assert parse_dt(1789639200).isoformat() == "2026-09-17T10:00:00+00:00"
    assert parse_dt("20260917T100000").isoformat() == "2026-09-17T10:00:00+00:00"
    assert parse_dt("2026-09-17T10:00:00Z").isoformat() == "2026-09-17T10:00:00+00:00"
    assert parse_dt("Thu, 17 Sep 2026 10:00:00 GMT").isoformat() == "2026-09-17T10:00:00+00:00"
    with pytest.raises(ValueError):
        parse_dt("yesterday")


def test_missing_key_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    with pytest.raises(SourceError, match="FINNHUB_API_KEY"):
        make_source("finnhub")
    with pytest.raises(SourceError, match="unknown source type"):
        make_source("nope")


@respx.mock
def test_finnhub():
    respx.get("https://finnhub.io/api/v1/company-news").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"datetime": 1789639200, "headline": "Apple beats", "summary": "s", "url": "https://x/1", "related": "AAPL"},
                {"datetime": 1700000000, "headline": "old", "url": "https://x/2"},
            ],
        )
    )
    out = run(make_source("finnhub", api_key="k"))
    assert [a.title for a in out] == ["Apple beats"]
    assert out[0].symbols == ("AAPL",) and out[0].source == "finnhub"


@respx.mock
def test_finnhub_http_error_becomes_source_error():
    respx.get("https://finnhub.io/api/v1/company-news").mock(return_value=httpx.Response(429, text="slow down"))
    with pytest.raises(SourceError) as exc:
        run(make_source("finnhub", api_key="k"))
    assert exc.value.status == 429


@respx.mock
def test_alpha_vantage_quota_message():
    respx.get("https://www.alphavantage.co/query").mock(return_value=httpx.Response(200, json={"Information": "quota"}))
    with pytest.raises(SourceError, match="quota"):
        run(make_source("alpha_vantage", api_key="k"))


@respx.mock
def test_alpha_vantage_feed():
    respx.get("https://www.alphavantage.co/query").mock(
        return_value=httpx.Response(
            200,
            json={"feed": [{"title": "T", "url": "https://x", "time_published": "20260917T100000", "summary": "S",
                            "ticker_sentiment": [{"ticker": "AAPL"}, {"ticker": "MSFT"}]}]},
        )
    )
    out = run(make_source("alpha_vantage", api_key="k"))
    assert out[0].symbols == ("AAPL", "MSFT")


@respx.mock
def test_polygon_follows_next_url():
    respx.get("https://api.massive.com/v2/reference/news", params__contains={"ticker": "AAPL"}).mock(
        return_value=httpx.Response(200, json={"results": [{"title": "p1", "published_utc": IN, "article_url": "https://x/1",
                                                            "tickers": ["AAPL"], "publisher": {"name": "Pub"}}],
                                               "next_url": "https://api.massive.com/v2/reference/news?cursor=abc"}),
    )
    respx.get("https://api.massive.com/v2/reference/news", params__contains={"cursor": "abc"}).mock(
        return_value=httpx.Response(200, json={"results": [{"title": "p2", "published_utc": IN, "article_url": "https://x/2"}]}),
    )
    out = run(make_source("polygon", api_key="k"))
    assert sorted(a.title for a in out) == ["p1", "p2"]
    assert out[0].raw["publisher_name"] in ("Pub", None)


@respx.mock
def test_tiingo_and_marketaux_and_newsapi():
    respx.get("https://api.tiingo.com/tiingo/news").mock(
        return_value=httpx.Response(200, json=[{"title": "t", "publishedDate": IN, "url": "https://t/1", "tickers": ["aapl"]}])
    )
    respx.get("https://api.marketaux.com/v1/news/all").mock(
        return_value=httpx.Response(200, json={"data": [{"title": "m", "published_at": IN, "url": "https://m/1",
                                                         "entities": [{"symbol": "AAPL"}]}],
                                               "meta": {"returned": 1, "limit": 3}})
    )
    respx.get("https://newsapi.org/v2/everything").mock(
        return_value=httpx.Response(200, json={"status": "ok", "articles": [
            {"title": "n", "publishedAt": IN, "url": "https://n/1", "source": {"name": "Reuters"}},
            {"title": "stale", "publishedAt": OUT, "url": "https://n/2"}]})
    )
    assert run(make_source("tiingo", api_key="k"))[0].symbols == ("AAPL",)
    assert run(make_source("marketaux", api_key="k"))[0].title == "m"
    news = run(make_source("newsapi", api_key="k"), query="Apple")
    assert [a.title for a in news] == ["n"]


@respx.mock
def test_newsapi_error_status():
    respx.get("https://newsapi.org/v2/everything").mock(
        return_value=httpx.Response(200, json={"status": "error", "message": "bad key"})
    )
    with pytest.raises(SourceError, match="bad key"):
        run(make_source("newsapi", api_key="k"), query="Apple")


RSS = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Apple rallies</title><link>https://r/1</link><description>&lt;p&gt;AAPL up&lt;/p&gt;</description>
<pubDate>Thu, 17 Sep 2026 10:00:00 GMT</pubDate></item>
<item><title>Unrelated</title><link>https://r/2</link><pubDate>Thu, 17 Sep 2026 11:00:00 GMT</pubDate></item>
</channel></rss>"""

ATOM = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>AAPL filing</title><link href="https://a/1"/><updated>2026-09-17T10:00:00Z</updated><summary>8-K</summary></entry>
</feed>"""


@respx.mock
def test_rss_filters_by_keyword_and_strips_html():
    respx.get("https://feed/x").mock(return_value=httpx.Response(200, text=RSS))
    out = run(make_source("rss", url="https://feed/x"))
    assert [a.title for a in out] == ["Apple rallies"]
    assert out[0].summary == "AAPL up"


@respx.mock
def test_rss_atom_and_template_url():
    route = respx.get("https://feed/AAPL").mock(return_value=httpx.Response(200, text=ATOM))
    out = run(make_source("rss", url="https://feed/{query}", match=False))
    assert route.called and out[0].url == "https://a/1" and out[0].summary == "8-K"


def test_rss_requires_url():
    with pytest.raises(SourceError, match="url"):
        make_source("rss")
