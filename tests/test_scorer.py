"""NewsScorer end to end with fake sources and user scoring functions."""

from __future__ import annotations

import asyncio
from datetime import datetime

import httpx
import pytest

from newsscore import Article, ArticleScore, NewsScorer, NewsSource, SourceError
from newsscore.config import SourceSpec, SourceStore

from conftest import make_article


class FakeSource(NewsSource):
    type_name = "fake"
    requires_key = False

    def __init__(self, articles, name="fake", fail=False):
        super().__init__(name=name)
        self.articles, self.fail = articles, fail

    async def fetch(self, query: str, since: datetime, until: datetime, client: httpx.AsyncClient) -> list[Article]:
        if self.fail:
            raise SourceError(self.name, "boom", 500)
        return list(self.articles)


def test_score_with_sync_user_function_and_source_selection():
    calls = []

    def fn(articles, query):
        calls.append(len(articles))
        return [0.5 if "beat" in a.title else -0.5 for a in articles]

    fn.name = "test-fn"
    scorer = NewsScorer(score_fn=fn, cache=False)
    scorer.source_add(FakeSource([make_article("beat", source="a"), make_article("miss", source="a", hours_ago=1)], name="a"))
    scorer.source_add(FakeSource([make_article("neutral", source="b")], name="b"))

    result = scorer.score("AAPL")
    assert result.n_articles == 3 and result.errors == []
    assert set(result.by_source) == {"a", "b"}
    assert result.articles[0].article.published >= result.articles[-1].article.published

    only_b = scorer.score("AAPL", sources=["b"])
    assert only_b.n_articles == 1 and only_b.score == -0.5
    with pytest.raises(KeyError):
        scorer.score("AAPL", sources=["zzz"])


def test_async_function_batching_dedupe_and_source_errors():
    seen_batches = []

    async def fn(articles, query):
        seen_batches.append(len(articles))
        return [{"score": 0.1, "confidence": 0.9, "labels": {"q": query}}] * len(articles)

    fn.name = "async-fn"
    dupes = [make_article("Apple Beats!", source="x", url="https://1"), make_article("apple beats", source="y", url="https://2")]
    scorer = NewsScorer(score_fn=fn, cache=False, batch_size=2)
    scorer.source_add(FakeSource(dupes + [make_article(f"n{i}", source="x") for i in range(3)], name="x"))
    scorer.source_add(FakeSource([], name="broken", fail=True))

    result = asyncio.run(scorer.ascore("AAPL", days=3))
    assert result.n_articles == 4  # one syndicated duplicate dropped
    assert seen_batches == [2, 2]
    assert result.errors == ["broken: boom (HTTP 500)"]
    assert result.articles[0].score.labels == {"q": "AAPL"}


def test_scorer_exception_is_recorded_not_raised():
    def fn(articles, query):
        raise RuntimeError("model down")

    fn.name = "bad"
    scorer = NewsScorer(score_fn=fn, cache=False)
    scorer.source_add(FakeSource([make_article("a")]))
    result = scorer.score("AAPL")
    assert result.n_articles == 0 and "model down" in result.errors[0]
    assert (result.score, result.confidence) == (0.0, 0.0)


def test_cache_avoids_rescoring(tmp_path):
    calls = []

    def fn(articles, query):
        calls.extend(a.id for a in articles)
        return [0.3] * len(articles)

    fn.name = "cached"
    arts = [make_article("a"), make_article("b")]
    for _ in range(2):
        scorer = NewsScorer(score_fn=fn, cache=tmp_path / "c.sqlite")
        scorer.source_add(FakeSource(arts))
        assert scorer.score("AAPL").n_articles == 2
        asyncio.run(scorer.aclose())
    assert len(calls) == 2  # second run served entirely from cache


def test_lambda_disables_cache_with_warning(caplog):
    scorer = NewsScorer(score_fn=lambda a, q: [0.0] * len(a))
    assert scorer.cache is None and scorer.scorer_name is None
    assert "caching disabled" in caplog.text


def test_no_sources_gives_helpful_error():
    result = NewsScorer(score_fn="keyword", cache=False).score("AAPL")
    assert result.n_articles == 0 and "no sources" in result.errors[0]


def test_source_add_validation():
    scorer = NewsScorer(score_fn="keyword", cache=False)
    scorer.source_add("yahoo")
    with pytest.raises(ValueError):
        scorer.source_add("yahoo")
    with pytest.raises(TypeError):
        scorer.source_add(FakeSource([]), api_key="x")
    scorer.source_remove("yahoo")
    assert scorer.sources == {}


def test_from_config_skips_unusable_sources(monkeypatch):
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    store = SourceStore()
    store.add(SourceSpec(type="yahoo", name="yahoo"))
    store.add(SourceSpec(type="finnhub", name="fh"))  # no key anywhere
    scorer = NewsScorer.from_config(score_fn="keyword", cache=False)
    assert list(scorer.sources) == ["yahoo"]


def test_sync_call_inside_running_loop_raises():
    async def go():
        NewsScorer(score_fn="keyword", cache=False).score("AAPL")

    with pytest.raises(RuntimeError, match="ascore"):
        asyncio.run(go())
