"""Expected impact: a separate judgement from sentiment, and opt-in for aggregation."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from newsscore import IMPACT_LEVELS, IMPACT_WEIGHTS, ArticleScore, NewsScorer, ScoredArticle
from newsscore.aggregate import aggregate
from newsscore.cli import app
from newsscore.config import SourceSpec, SourceStore

from conftest import NOW, make_article
from test_failures import ListSource

runner = CliRunner()


def _scored(impact, score=0.5, hours_ago=1):
    return ScoredArticle(
        article=make_article(f"AAPL news {impact} {hours_ago}", hours_ago=hours_ago),
        score=ArticleScore(score=score, expected_impact=impact),
    )


def _impact_scorer(mapping, scores=None):
    """A scorer that labels impact from a marker in the title, not from the sentiment.

    `scores` lets a test give articles opposing sentiment, which is the only way a
    change in weighting can move a weighted mean at all.
    """
    scores = scores or {}

    def fn(articles, query):
        return [
            ArticleScore(score=scores.get(a.title, 0.5), expected_impact=mapping.get(a.title))
            for a in articles
        ]

    fn.name = "impact-fn"
    return fn


# ---- the field ------------------------------------------------------------------------


def test_impact_levels_are_validated():
    assert IMPACT_LEVELS == ("low", "medium", "high")
    for level in IMPACT_LEVELS:
        assert ArticleScore(score=0.0, expected_impact=level).expected_impact == level
    assert ArticleScore(score=0.0).expected_impact is None
    with pytest.raises(ValueError, match="expected_impact"):
        ArticleScore(score=0.0, expected_impact="enormous")


def test_impact_is_independent_of_sentiment():
    """The doc's example: bearish+high and bullish+low must both be expressible."""
    lawsuit = ArticleScore(score=-0.95, expected_impact="high")
    contract = ArticleScore(score=0.3, expected_impact="low")
    assert lawsuit.expected_impact == "high" and contract.expected_impact == "low"


def test_impact_survives_serialisation():
    """Criterion 12, first half: label and probabilities round-trip."""
    original = ArticleScore(
        score=-0.9, expected_impact="high", labels={"impact_probs": {"high": 0.7, "low": 0.3}}
    )
    restored = ArticleScore.from_dict(json.loads(json.dumps(original.to_dict())))
    assert restored.expected_impact == "high"
    assert restored.labels["impact_probs"] == {"high": 0.7, "low": 0.3}


def test_impact_survives_the_cache(data_dir):
    cache = data_dir / "c.sqlite"
    arts = [make_article("big", hours_ago=1).to_dict()]
    fn = _impact_scorer({"big": "high"})

    first = NewsScorer(score_fn=fn, cache=cache).score_articles(arts, "AAPL", as_of=NOW)
    second = NewsScorer(score_fn=fn, cache=cache).score_articles(arts, "AAPL", as_of=NOW)
    assert first.articles[0].score.expected_impact == "high"
    assert second.articles[0].score.labels["local_cache_hit"] is True
    assert second.articles[0].score.expected_impact == "high", "cached scores keep their impact"


# ---- aggregation ----------------------------------------------------------------------


def test_impact_is_ignored_unless_weighting_is_enabled():
    """Criterion 12, second half: labels alone must not change the aggregate."""
    items = [_scored("high", score=1.0, hours_ago=1), _scored("low", score=-1.0, hours_ago=1)]
    off = aggregate(items, NOW)
    on = aggregate(items, NOW, impact_weights=IMPACT_WEIGHTS)
    assert off.score == pytest.approx(0.0), "without weighting the two cancel out"
    # high=3.0 against low=1.0: (3*1 + 1*-1) / 4
    assert on.score == pytest.approx(0.5), "with weighting the high-impact item dominates"


def test_missing_impact_weighs_one():
    """Decision: a scorer that cannot judge impact keeps working, unchanged."""
    labelled = [_scored("high", score=1.0), _scored(None, score=-1.0)]
    unlabelled = [
        ScoredArticle(a.article, ArticleScore(score=a.score.score)) for a in labelled
    ]
    assert aggregate(unlabelled, NOW, impact_weights=IMPACT_WEIGHTS).score == pytest.approx(
        aggregate(unlabelled, NOW).score
    ), "with no impact anywhere, enabling weighting changes nothing"

    mixed = aggregate(labelled, NOW, impact_weights=IMPACT_WEIGHTS)
    assert mixed.score == pytest.approx((3.0 * 1.0 + 1.0 * -1.0) / 4.0, abs=1e-9)


def test_keyword_scorer_still_works_with_impact_weighting_on():
    result = NewsScorer(
        score_fn="keyword", cache=False, impact_weights=IMPACT_WEIGHTS
    ).score_articles([make_article("AAPL beats estimates", hours_ago=1).to_dict()], "AAPL", as_of=NOW)
    assert result.status == "ok"
    assert result.articles[0].score.expected_impact is None


def test_bad_impact_mapping_is_rejected():
    with pytest.raises(ValueError, match="impact_weights"):
        aggregate([_scored("high")], NOW, impact_weights={"enormous": 9.0})


def test_quant_mapping_matches_the_documented_values():
    assert IMPACT_WEIGHTS == {"high": 3.0, "medium": 1.5, "low": 1.0}


# ---- CLI ------------------------------------------------------------------------------


def _save_source(articles):
    ListSource.articles = articles
    SourceStore().add(SourceSpec(type="listsource", name="s"), replace=True)


def test_cli_exposes_impact_in_json_and_respects_the_flag(monkeypatch):
    """Criterion 12 end to end: impact reaches the CLI, and only weighs when asked."""
    from newsscore.scoring import SCORERS

    mapping = {"big": "high", "small": "low"}
    sentiment = {"big": 1.0, "small": -1.0}  # opposing, so weighting is observable
    monkeypatch.setitem(SCORERS, "impact-test", lambda **kw: _impact_scorer(mapping, sentiment))
    _save_source([make_article("big", hours_ago=1), make_article("small", hours_ago=1)])

    base = ["score", "AAPL", "-d", "30", "--json", "--scorer", "impact-test", "--no-cache"]
    plain = runner.invoke(app, base)
    weighted = runner.invoke(app, [*base, "--impact-weighting"])

    assert plain.exit_code == 0, plain.stdout
    assert weighted.exit_code == 0, weighted.stdout
    payload = json.loads(plain.stdout)
    assert {a["expected_impact"] for a in payload["articles"]} == {"high", "low"}
    assert json.loads(weighted.stdout)["score"] != pytest.approx(payload["score"]), (
        "the flag must actually change the aggregate"
    )


# ---- the impact question is optional ---------------------------------------------------


def test_impact_question_can_be_switched_on():
    """Asking for impact costs tokens, so a caller opts in when they will weight by it."""
    from newsscore import JevScorer

    on = JevScorer(api_key="k", impact=True)
    off = JevScorer(api_key="k")
    assert on.asks_impact and not off.asks_impact
    assert "impact" in on._question_spec()
    assert "impact" not in off._question_spec()


def test_asking_or_not_is_part_of_the_cache_identity():
    """The two ask different things, so they must not share cached answers."""
    from newsscore import JevScorer

    assert JevScorer(api_key="k", impact=True).fingerprint != JevScorer(api_key="k").fingerprint


def test_impact_rubric_is_configurable_and_validated():
    from newsscore import JevScorer
    from newsscore.scoring.jev import IMPACT_RUBRIC

    reworded = {**IMPACT_RUBRIC, "high": "Anything a PM would act on before the next open."}
    asked = JevScorer(api_key="k", impact=True)
    assert JevScorer(api_key="k", impact=True, impact_rubric=reworded).fingerprint != asked.fingerprint

    with pytest.raises(ValueError, match="impact_rubric"):
        JevScorer(api_key="k", impact_rubric={"small": "x", "big": "y"})


def test_the_impact_question_is_off_by_default():
    """It costs ~24% more tokens and is inert unless impact_weights is also on.

    Measured over 308 AAPL articles: the label is reproducible (Cohen's kappa 0.92)
    and not implied by sentiment, but nothing validates it against realised moves,
    so the question and the weighting are opted into together rather than apart.
    """
    from newsscore import JevScorer

    assert JevScorer(api_key="k").asks_impact is False
    assert JevScorer(api_key="k", impact=True).asks_impact is True


def test_scorer_options_reach_a_scorer_named_by_string(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")  # conftest strips it; preflight wants one
    scorer = NewsScorer(score_fn="jev", cache=False, scorer_options={"impact": False})
    assert scorer.score_fn.asks_impact is False


def test_scorer_options_are_rejected_for_an_instance():
    from newsscore import KeywordScorer

    with pytest.raises(TypeError, match="scorer_options"):
        NewsScorer(score_fn=KeywordScorer(), cache=False, scorer_options={"impact": False})


def test_cli_no_impact_reports_clearly_for_a_scorer_without_it():
    _save_source([make_article("AAPL beats estimates", hours_ago=1)])
    result = runner.invoke(app, ["score", "AAPL", "-d", "30", "--scorer", "keyword", "--impact"])
    assert result.exit_code == 1
    assert "--impact does not apply" in result.stderr


def test_impact_weighting_without_the_question_says_so():
    """The two switches are independent, so asking for one alone does nothing."""
    _save_source([make_article("AAPL beats estimates", hours_ago=1)])
    args = ["score", "AAPL", "-d", "30", "--scorer", "keyword", "--no-cache"]
    quiet = runner.invoke(app, args)
    warned = runner.invoke(app, [*args, "--impact-weighting"])
    assert "had no effect" not in quiet.stderr
    assert "--impact-weighting had no effect" in warned.stderr
    assert warned.exit_code == quiet.exit_code, "a warning, not a failure"
