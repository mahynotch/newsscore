"""Cache identity and provenance: what a stored score is allowed to be reused for."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest

from newsscore import JevScorer, NewsScorer
from newsscore.cache import ScoreCache
from newsscore.scoring.jev import DEFAULT_MODEL, is_pinned

from conftest import NOW, make_article

KEY = "test-key-not-used"  # conftest strips the real one; preflight only checks presence


class Contract:
    """A scorer whose contract fingerprint can be changed between runs."""

    name = "contract-scorer"

    def __init__(self, fingerprint="v1"):
        self.fingerprint = fingerprint
        self.calls: list[tuple[str, str]] = []

    def __call__(self, articles, query):
        self.calls.extend((a.id, query) for a in articles)
        return [0.5] * len(articles)


def _run(scorer, articles, query="AAPL"):
    result = scorer.score_articles([a.to_dict() for a in articles], query, as_of=NOW)
    asyncio.run(scorer.aclose())
    return result


# ---- what invalidates a cached score ---------------------------------------------------


def test_edited_content_is_rescored_under_the_same_id(data_dir):
    """Criterion 2: article content is part of the identity, not just its id."""
    cache = data_dir / "c.sqlite"
    original = make_article("AAPL beats estimates", url="https://x/1", hours_ago=2)
    edited = make_article("AAPL misses estimates", url="https://x/1", hours_ago=2)
    assert original.id == edited.id, "same url, so the same id: only the text changed"
    assert original.content_digest != edited.content_digest

    first = Contract()
    _run(NewsScorer(score_fn=first, cache=cache), [original])
    assert len(first.calls) == 1

    second = Contract()
    _run(NewsScorer(score_fn=second, cache=cache), [edited])
    assert len(second.calls) == 1, "an edited headline must not reuse the old score"

    third = Contract()
    _run(NewsScorer(score_fn=third, cache=cache), [original])
    assert third.calls == [], "the untouched article is still cached"


def test_changed_contract_cannot_reuse_old_answers(data_dir):
    """Criterion 2: a new rubric means the model answered a different question."""
    cache = data_dir / "c.sqlite"
    article = make_article("AAPL beats estimates", hours_ago=2)

    before = Contract(fingerprint="rubric-v1")
    _run(NewsScorer(score_fn=before, cache=cache), [article])
    assert len(before.calls) == 1

    same = Contract(fingerprint="rubric-v1")
    _run(NewsScorer(score_fn=same, cache=cache), [article])
    assert same.calls == [], "unchanged contract still hits the cache"

    after = Contract(fingerprint="rubric-v2")
    _run(NewsScorer(score_fn=after, cache=cache), [article])
    assert len(after.calls) == 1, "a changed rubric must invalidate the entry"


def test_entries_written_by_the_old_schema_are_never_served(data_dir):
    """Criterion: 0.1 rows came from a different contract; reusing them would be wrong."""
    cache = data_dir / "c.sqlite"
    article = make_article("AAPL beats estimates", hours_ago=2)

    with ScoreCache(cache):  # create the file, then forge a 0.1-shaped row
        pass
    conn = sqlite3.connect(cache)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS scores (scorer TEXT, query TEXT, article_id TEXT,"
        " payload TEXT, created REAL, PRIMARY KEY (scorer, query, article_id))"
    )
    conn.execute(
        "INSERT INTO scores VALUES (?,?,?,?,?)",
        ("contract-scorer", "AAPL", article.id, json.dumps({"score": -0.9}), 0.0),
    )
    conn.commit()
    conn.close()

    scorer = Contract()
    result = _run(NewsScorer(score_fn=scorer, cache=cache), [article])
    assert len(scorer.calls) == 1, "the legacy row must not be reused"
    assert result.articles[0].score.score == 0.5, "the fresh score wins, not the old -0.9"


def test_cache_clear_removes_both_schemas(data_dir):
    cache = data_dir / "c.sqlite"
    article = make_article("AAPL beats estimates", hours_ago=2)
    _run(NewsScorer(score_fn=Contract(), cache=cache), [article])

    conn = sqlite3.connect(cache)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS scores (scorer TEXT, query TEXT, article_id TEXT,"
        " payload TEXT, created REAL, PRIMARY KEY (scorer, query, article_id))"
    )
    conn.execute("INSERT INTO scores VALUES ('old','AAPL','x','{}',0.0)")
    conn.commit()
    conn.close()

    with ScoreCache(cache) as store:
        assert store.clear() == 2, "one v2 row and one legacy row"


# ---- model identity --------------------------------------------------------------------


def test_default_model_is_pinned_not_an_alias():
    """Decision: cached scores must always belong to a known model version."""
    assert is_pinned(DEFAULT_MODEL) and not is_pinned("jev-latest")
    assert JevScorer(api_key=KEY).model == DEFAULT_MODEL
    assert JevScorer(api_key=KEY).fingerprint is not None


def test_mutable_alias_refuses_caching(caplog):
    """You only learn which model answered after paying, so an alias cannot be a key."""
    assert JevScorer(api_key=KEY, model="jev-latest").fingerprint is None
    scorer = NewsScorer(score_fn=JevScorer(api_key=KEY, model="jev-latest"), cache=True)
    assert scorer.cache is None
    assert "declines caching" in caplog.text


def test_changing_the_model_changes_the_fingerprint():
    assert (
        JevScorer(api_key=KEY, model="jev-1.13.0").fingerprint
        != JevScorer(api_key=KEY, model="jev-2.0.0").fingerprint
    )


def test_keyword_fingerprint_tracks_its_lexicon(monkeypatch):
    from newsscore.scoring import keyword

    before = keyword.KeywordScorer().fingerprint
    monkeypatch.setattr(keyword, "POSITIVE", frozenset({*keyword.POSITIVE, "moonshot"}))
    assert keyword.KeywordScorer().fingerprint != before


# ---- provenance ------------------------------------------------------------------------


def _response(model="jev-1.13.0"):
    return SimpleNamespace(
        model=model,
        request_id="req_abc123",
        usage=SimpleNamespace(input_tokens=300, output_tokens=20),
        answers={
            "sentiment": SimpleNamespace(score=4, confidence=0.9, probabilities={4: 1.0}),
            "relevant": SimpleNamespace(noul=0.8),
            "category": SimpleNamespace(choice="earnings", probabilities={"earnings": 1.0}),
            "novel": SimpleNamespace(noul=0.5),
        },
    )


def test_jev_records_what_was_asked_and_what_answered():
    out = JevScorer._convert(_response(), requested_model="jev-latest", latency_ms=942)
    assert out.labels["requested_model"] == "jev-latest"
    assert out.labels["model"] == "jev-1.13.0", "the version that actually answered"
    assert out.labels["request_id"] == "req_abc123"
    assert out.labels["latency_ms"] == 942
    assert out.labels["usage"] == {"input_tokens": 300, "output_tokens": 20}
    assert out.labels["sentiment_probs"] == {4: 1.0}


def test_cache_hit_is_flagged_and_costs_no_new_tokens(data_dir):
    """Criterion 11: a local hit makes no model call and is not billed to this run."""
    cache = data_dir / "c.sqlite"
    article = make_article("AAPL beats estimates", hours_ago=2)

    def fn(articles, query):
        return [{"score": 0.5, "labels": {"usage": {"input_tokens": 100, "output_tokens": 10}}}] * len(articles)

    fn.name = "usage-fn"
    fn.fingerprint = "v1"

    first = _run(NewsScorer(score_fn=fn, cache=cache), [article])
    assert first.usage == {"input_tokens": 100, "output_tokens": 10, "requests": 1}
    assert first.articles[0].score.labels["local_cache_hit"] is False

    second = _run(NewsScorer(score_fn=fn, cache=cache), [article])
    assert second.articles[0].score.labels["local_cache_hit"] is True
    assert second.usage == {}, "a cache hit must not be reported as new consumption"
    assert second.articles[0].score.labels["usage"] == {"input_tokens": 100, "output_tokens": 10}, (
        "what it originally cost is kept as provenance"
    )


def test_usage_survives_serialisation(data_dir):
    def fn(articles, query):
        return [{"score": 0.5, "labels": {"usage": {"input_tokens": 7}}}] * len(articles)

    fn.name = "usage-fn"
    result = _run(NewsScorer(score_fn=fn, cache=False), [make_article("AAPL beats", hours_ago=1)])
    assert json.loads(json.dumps(result.to_dict()))["usage"] == {"input_tokens": 7, "requests": 1}


# ---- confidence semantics --------------------------------------------------------------


def test_evidence_confidence_is_named_and_distinct_from_article_confidence():
    result = NewsScorer(score_fn="keyword", cache=False).score_articles(
        [make_article("AAPL beats estimates strongly", hours_ago=1).to_dict()], "AAPL", as_of=NOW
    )
    assert result.evidence_confidence == result.confidence, "documented compatibility path"
    article_confidence = result.articles[0].score.confidence
    assert result.evidence_confidence != pytest.approx(article_confidence), (
        "aggregate evidence confidence is a different quantity from the scorer's own"
    )
