"""Configurable aggregation and its audit trail."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from typer.testing import CliRunner

from newsscore import (
    IMPACT_WEIGHTS,
    ArticleScore,
    NewsScorer,
    ScoredArticle,
    aggregate,
    quant_aggregator,
)
from newsscore.cli import app
from newsscore.config import SourceSpec, SourceStore

from conftest import NOW, make_article
from test_failures import ListSource

runner = CliRunner()


def _item(score=0.5, *, hours_ago=1, confidence=1.0, relevance=1.0, impact=None, source="test"):
    return ScoredArticle(
        article=make_article(f"AAPL {score} {hours_ago} {source}", source=source, hours_ago=hours_ago),
        score=ArticleScore(
            score=score, confidence=confidence, relevance=relevance, expected_impact=impact
        ),
    )


# ---- controls --------------------------------------------------------------------------


def test_relevance_can_be_switched_off():
    items = [_item(1.0, relevance=0.1), _item(-1.0, relevance=1.0, hours_ago=1.0001)]
    with_rel = aggregate(items, NOW)
    without = aggregate(items, NOW, use_relevance=False)
    assert with_rel.score < -0.5, "the relevant bearish item dominates by default"
    assert without.score == pytest.approx(0.0, abs=1e-3), "without relevance they cancel"


def test_lookback_excludes_old_articles_entirely():
    items = [_item(1.0, hours_ago=1), _item(-1.0, hours_ago=100)]
    assert aggregate(items, NOW, half_life_hours=0).score == pytest.approx(0.0)
    bounded = aggregate(items, NOW, half_life_hours=0, lookback_hours=48)
    assert bounded.score == pytest.approx(1.0), "the 100h-old article is out of scope"
    assert len(bounded.contributions) == 1


def test_future_dated_articles_are_excluded_not_treated_as_freshest():
    """They used to clamp to age 0, i.e. maximum decay weight."""
    future = _item(-1.0, hours_ago=-10)
    present = _item(1.0, hours_ago=1)
    result = aggregate([future, present], NOW)
    assert result.score == pytest.approx(1.0)
    assert [c.article_id for c in result.contributions] == [present.article.id]


def test_half_life_is_configurable():
    items = [_item(1.0, hours_ago=1), _item(-1.0, hours_ago=48)]
    assert aggregate(items, NOW, half_life_hours=2).score > aggregate(items, NOW, half_life_hours=240).score


def test_as_of_makes_replay_deterministic():
    items = [_item(1.0, hours_ago=1), _item(-1.0, hours_ago=30)]
    first = aggregate(items, NOW)
    assert aggregate(items, NOW).score == pytest.approx(first.score), "same as_of, same answer"

    # Moving as_of forward scales every weight by the same 0.5**(delta/half_life), and
    # a weighted mean is invariant under a common factor: the score is unchanged and
    # only the evidence confidence falls, because there is less total weight.
    later = aggregate(items, NOW + timedelta(hours=5))
    assert later.score == pytest.approx(first.score)
    assert later.confidence < first.confidence
    assert later.total_weight < first.total_weight

    # With a lookback it does move, because an article can age out of the window.
    near = aggregate(items, NOW, lookback_hours=32)
    far = aggregate(items, NOW + timedelta(hours=5), lookback_hours=32)
    assert len(near.contributions) == 2 and len(far.contributions) == 1
    assert far.score != pytest.approx(near.score)


# ---- diagnostics -----------------------------------------------------------------------


def test_contributions_reconcile_to_the_reported_score():
    """Criterion 8: the per-article shares sum to the aggregate."""
    items = [
        _item(0.8, confidence=0.9, relevance=0.7, hours_ago=2),
        _item(-0.4, confidence=0.5, relevance=1.0, hours_ago=20),
        _item(0.1, confidence=1.0, relevance=0.3, hours_ago=40),
    ]
    result = aggregate(items, NOW)
    assert sum(c.contribution for c in result.contributions) == pytest.approx(result.score, abs=1e-12)


def test_contributions_reconcile_with_every_term_enabled():
    items = [
        _item(1.0, confidence=0.8, relevance=0.9, impact="high", hours_ago=3),
        _item(-0.6, confidence=0.6, relevance=0.4, impact="low", hours_ago=30),
        _item(0.2, confidence=1.0, relevance=1.0, impact="medium", hours_ago=10),
    ]
    result = aggregate(items, NOW, impact_weights=IMPACT_WEIGHTS, lookback_hours=48)
    assert sum(c.contribution for c in result.contributions) == pytest.approx(result.score, abs=1e-12)


def test_every_weight_term_is_exposed():
    item = _item(0.5, confidence=0.8, relevance=0.6, impact="high", hours_ago=48)
    [row] = aggregate([item], NOW, impact_weights=IMPACT_WEIGHTS, half_life_hours=48).contributions
    assert row.article_id == item.article.id and row.source == "test"
    assert row.score == 0.5 and row.confidence == 0.8 and row.relevance == 0.6
    assert row.impact == 3.0
    assert row.decay == pytest.approx(0.5), "one half-life old"
    assert row.weight == pytest.approx(0.8 * 0.6 * 3.0 * 0.5)
    assert row.contribution == pytest.approx(0.5), "the only article carries the whole score"


def test_relevance_term_reports_one_when_disabled():
    [row] = aggregate([_item(0.5, relevance=0.25)], NOW, use_relevance=False).contributions
    assert row.relevance == 1.0, "what was actually used, not what the scorer said"


def test_zero_weight_is_distinguishable_from_neutral():
    """Criterion 6: 0.0 from no evidence is not 0.0 from balanced evidence."""
    neutral = NewsScorer(score_fn=lambda a, q: [0.0] * len(a), scorer_name="n", cache=False)
    weightless = NewsScorer(
        score_fn=lambda a, q: [ArticleScore(score=0.9, confidence=0.0)] * len(a),
        scorer_name="w",
        cache=False,
    )
    arts = [make_article("AAPL news", hours_ago=1).to_dict()]
    good = neutral.score_articles(arts, "AAPL", as_of=NOW)
    empty = weightless.score_articles(arts, "AAPL", as_of=NOW)

    assert good.score == empty.score == 0.0
    assert good.status == "ok" and good.total_weight > 0
    assert empty.status == "no_weight" and empty.total_weight == 0.0
    assert empty.contributions == []


def test_result_reconciles_and_serialises():
    result = NewsScorer(score_fn="keyword", cache=False).score_articles(
        [
            make_article("AAPL beats estimates strongly", hours_ago=2).to_dict(),
            make_article("AAPL faces lawsuit and probe", hours_ago=8).to_dict(),
        ],
        "AAPL",
        as_of=NOW,
    )
    assert result.reconciles()
    payload = json.loads(json.dumps(result.to_dict(), default=str))
    assert len(payload["contributions"]) == 2
    assert payload["total_weight"] > 0
    assert sum(c["contribution"] for c in payload["contributions"]) == pytest.approx(payload["score"])
    assert {c["article_id"] for c in payload["contributions"]} == {
        a.article.id for a in result.articles
    }


def test_custom_aggregator_reports_no_contributions_and_still_reconciles():
    """Decision: summing to the score is a property of a weighted mean, not of all
    aggregation, so a custom fn is not asked to provide it."""
    from newsscore import Aggregate

    scorer = NewsScorer(
        score_fn="keyword", cache=False, aggregate_fn=lambda scored, now: Aggregate(score=0.42, confidence=0.5)
    )
    result = scorer.score_articles([make_article("AAPL beats", hours_ago=1).to_dict()], "AAPL", as_of=NOW)
    assert result.score == 0.42 and result.contributions == []
    assert result.reconciles(), "vacuously true without contributions"
    assert result.status == "ok", "a custom aggregator falls back to per-article weights"


# ---- the quant compatibility preset ----------------------------------------------------


def test_quant_aggregator_matches_its_documented_scheme():
    item = _item(1.0, confidence=0.8, relevance=0.2, impact="high", hours_ago=12)
    [row] = quant_aggregator()([item], NOW).contributions
    assert row.relevance == 1.0, "no relevance term"
    assert row.impact == 3.0
    assert row.decay == pytest.approx(0.5), "12-hour half-life, 12 hours old"
    assert row.weight == pytest.approx(0.8 * 3.0 * 0.5)


def test_quant_aggregator_applies_a_48_hour_lookback():
    assert quant_aggregator()([_item(1.0, hours_ago=60)], NOW).contributions == []


def test_presets_differ_without_any_model_call(data_dir):
    """Criterion 3: swapping aggregation re-reads stored scores, nothing else."""
    calls: list = []

    def fn(articles, query):
        calls.extend(a.id for a in articles)
        return [
            ArticleScore(score=1.0 if "beats" in a.title else -1.0, confidence=0.9, relevance=0.3,
                         expected_impact="high" if "lawsuit" in a.title else "low")
            for a in articles
        ]

    fn.name = "preset-fn"
    arts = [
        make_article("AAPL beats estimates", hours_ago=2).to_dict(),
        make_article("AAPL lawsuit filed", hours_ago=30).to_dict(),
    ]
    base = NewsScorer(score_fn=fn, cache=False).score_articles(arts, "AAPL", as_of=NOW)
    assert len(calls) == 2

    calls.clear()
    saved = base.to_dict()
    quant = NewsScorer(score_fn=fn, cache=False, aggregate_fn=quant_aggregator()).aggregate_scored(
        saved["articles"], "AAPL", as_of=saved["until"]
    )
    assert calls == [], "re-aggregating must not score anything again"
    assert quant.score != pytest.approx(base.score), "the two schemes really do differ"
    assert quant.reconciles()


# ---- CLI --------------------------------------------------------------------------------


def test_cli_exposes_the_new_controls():
    ListSource.articles = [
        make_article("AAPL beats estimates", hours_ago=2),
        make_article("AAPL lawsuit probe warning", hours_ago=60),
    ]
    SourceStore().add(SourceSpec(type="listsource", name="s"), replace=True)
    base = ["score", "AAPL", "-d", "30", "--scorer", "keyword", "--no-cache", "--json"]

    plain = json.loads(runner.invoke(app, base).stdout)
    bounded = json.loads(runner.invoke(app, [*base, "--lookback", "48"]).stdout)
    no_rel = json.loads(runner.invoke(app, [*base, "--no-relevance"]).stdout)

    assert len(plain["contributions"]) == 2
    assert len(bounded["contributions"]) == 1, "--lookback drops the 60h-old article"
    assert sum(c["contribution"] for c in bounded["contributions"]) == pytest.approx(bounded["score"])
    assert no_rel["contributions"][0]["relevance"] == 1.0
