"""Models, scoring contract, aggregation and the keyword scorer (no network)."""

from __future__ import annotations

import asyncio

import pytest

from newsscore import ArticleScore, KeywordScorer, ScoredArticle, aggregate, per_article
from newsscore.scoring.protocol import call_score_fn, normalise_scores, scorer_name

from conftest import NOW, make_article


def test_article_score_validates_ranges():
    with pytest.raises(ValueError):
        ArticleScore(score=1.5)
    with pytest.raises(ValueError):
        ArticleScore(score=0, confidence=-0.1)
    s = ArticleScore(score=0.5, confidence=0.5, relevance=0.5)
    assert s.weight == 0.25
    assert ArticleScore.from_dict(s.to_dict()) == s


def test_normalise_accepts_all_forms():
    out = normalise_scores([0.2, ArticleScore(-0.3), {"score": 0.9, "confidence": 0.4}], 3)
    assert [s.score for s in out] == [0.2, -0.3, 0.9]
    assert out[2].confidence == 0.4


@pytest.mark.parametrize("bad", [[0.1, 0.2], "abc", [True], [{"confidence": 1}], [object()]])
def test_normalise_rejects_bad_output(bad):
    with pytest.raises(ValueError):
        normalise_scores(bad, 1)


def test_call_score_fn_sync_and_async():
    arts = [make_article("a"), make_article("b")]

    def sync_fn(articles, query):
        return [0.1] * len(articles)

    async def async_fn(articles, query):
        return [{"score": -0.1}] * len(articles)

    assert [s.score for s in asyncio.run(call_score_fn(sync_fn, arts, "AAPL"))] == [0.1, 0.1]
    assert [s.score for s in asyncio.run(call_score_fn(async_fn, arts, "AAPL"))] == [-0.1, -0.1]


def test_per_article_adapter_and_names():
    def rule(article, query):
        return 1.0 if "beat" in article.text else 0.0

    fn = per_article(rule)
    out = asyncio.run(call_score_fn(fn, [make_article("beat"), make_article("miss")], "AAPL"))
    assert [s.score for s in out] == [1.0, 0.0]
    assert scorer_name(fn) is None or "rule" in scorer_name(fn)
    assert scorer_name(lambda a, q: []) is None
    assert scorer_name(KeywordScorer()) == "keyword-v1"


def test_aggregate_weights_and_decay():
    fresh = ScoredArticle(make_article("fresh"), ArticleScore(1.0))
    old = ScoredArticle(make_article("old", hours_ago=48), ArticleScore(-1.0))
    agg = aggregate([fresh, old], NOW, half_life_hours=48)
    # weights 1.0 and 0.5 -> (1 - 0.5) / 1.5
    assert agg.score == pytest.approx(1 / 3)
    assert 0 < agg.confidence < 1
    assert agg.by_source == {"test": pytest.approx(1 / 3)}

    empty = aggregate([], NOW)
    assert (empty.score, empty.confidence) == (0.0, 0.0)

    zero_weight = ScoredArticle(make_article("x"), ArticleScore(0.9, confidence=0.0))
    assert aggregate([zero_weight], NOW).confidence == 0.0


def test_keyword_scorer_direction_and_negation():
    scorer = KeywordScorer()
    arts = [
        make_article("Apple beats estimates, raises guidance"),
        make_article("Apple misses estimates amid weak demand"),
        make_article("Apple did not beat estimates"),
        make_article("Company X reports results"),
    ]
    out = scorer(arts, "AAPL")
    assert out[0].score > 0 > out[1].score
    assert out[2].score < 0
    assert out[3].confidence == 0.0
    assert out[0].relevance == 1.0  # symbol matches
