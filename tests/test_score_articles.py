"""Scoring articles the caller already has, and re-aggregating saved scores."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import pytest
from typer.testing import CliRunner

from newsscore import Article, NewsScorer, ScoredArticle
from newsscore.cli import app

from conftest import NOW, make_article

runner = CliRunner()


def _counting_scorer(calls: list, **kwargs):
    def fn(articles, query):
        calls.extend((a.id, query) for a in articles)
        return [0.5] * len(articles)

    fn.name = "counting"
    return NewsScorer(score_fn=fn, **kwargs)


def _payload():
    return [
        make_article("AAPL beats estimates", hours_ago=2).to_dict(),
        make_article("AAPL upgraded by analysts", hours_ago=5).to_dict(),
    ]


# ---- the interface itself --------------------------------------------------------------


def test_scores_supplied_articles_without_any_source():
    scorer = NewsScorer(score_fn="keyword", cache=False)
    assert scorer.sources == {}, "no fetching involved"
    result = scorer.score_articles(_payload(), "AAPL", as_of=NOW)
    assert result.n_articles == 2 and result.status == "ok"


def test_article_identity_is_preserved_not_regenerated():
    """Criterion: ids, titles, times, sources and urls come back exactly as supplied."""
    supplied = {
        "id": "caller-supplied-id",
        "source": "internal-wire",
        "title": "Apple beats estimates",
        "published": "2026-09-17T14:00:00Z",
        "url": "https://example.invalid/a",
        "summary": "Strong quarter.",
        "symbols": ["aapl"],
    }
    result = NewsScorer(score_fn="keyword", cache=False).score_articles([supplied], "AAPL", as_of=NOW)
    got = result.articles[0].article
    assert got.id == "caller-supplied-id"
    assert got.source == "internal-wire" and got.url == supplied["url"]
    assert got.title == "Apple beats estimates" and got.symbols == ("AAPL",)
    assert got.published.isoformat() == "2026-09-17T14:00:00+00:00"


def test_accepts_article_objects_and_mappings_alike():
    arts = [make_article("AAPL beats estimates", hours_ago=1)]
    scorer = NewsScorer(score_fn="keyword", cache=False)
    from_objects = scorer.score_articles(arts, "AAPL", as_of=NOW)
    from_dicts = scorer.score_articles([a.to_dict() for a in arts], "AAPL", as_of=NOW)
    assert from_objects.score == from_dicts.score
    assert from_objects.articles[0].article.id == from_dicts.articles[0].article.id


def test_malformed_article_names_the_offender():
    with pytest.raises(ValueError, match="title"):
        NewsScorer(score_fn="keyword", cache=False).score_articles([{"published": "2026-09-17"}], "AAPL")


# ---- reconciliation --------------------------------------------------------------------


def test_counts_account_for_every_supplied_item():
    arts = [
        make_article("AAPL beats estimates", hours_ago=2),
        make_article("AAPL beats estimates!", source="wire", hours_ago=3),  # syndicated copy
        make_article("AAPL guidance cut", hours_ago=-6),  # dated after as_of
        make_article("AAPL upgraded", hours_ago=4),
    ]
    result = NewsScorer(score_fn="keyword", cache=False).score_articles(
        [a.to_dict() for a in arts], "AAPL", as_of=NOW
    )
    c = result.counts
    assert c.fetched == 4
    assert c.deduplicated == 1 and c.filtered == 1
    assert c.fetched == c.deduplicated + c.filtered + c.submitted
    assert c.submitted == c.scored + c.failed


def test_future_dated_articles_are_filtered_not_treated_as_fresh():
    """Criterion 7: a fixed as_of excludes the future instead of giving it full weight."""
    future = make_article("AAPL beats estimates", hours_ago=-48)
    scorer = NewsScorer(score_fn="keyword", cache=False)
    result = scorer.score_articles([future.to_dict()], "AAPL", as_of=NOW)
    assert result.counts.filtered == 1 and result.n_articles == 0
    assert result.status == "no_articles"

    later = scorer.score_articles([future.to_dict()], "AAPL", as_of=NOW + timedelta(hours=72))
    assert later.counts.filtered == 0 and later.n_articles == 1


def test_dedupe_can_be_turned_off():
    arts = [make_article("AAPL beats estimates", hours_ago=2), make_article("AAPL beats estimates", source="wire", hours_ago=3)]
    scorer = NewsScorer(score_fn="keyword", cache=False)
    assert scorer.score_articles([a.to_dict() for a in arts], "AAPL", as_of=NOW, dedupe=False).n_articles == 2
    assert scorer.score_articles([a.to_dict() for a in arts], "AAPL", as_of=NOW).n_articles == 1


# ---- one article, two targets ----------------------------------------------------------


def test_same_article_for_two_targets_is_two_cached_tasks(data_dir):
    """Criterion 1: independently identified and cached per (article, target)."""
    calls: list = []
    cache = data_dir / "c.sqlite"
    payload = _payload()

    first = _counting_scorer(calls, cache=cache)
    first.score_articles(payload, "AAPL", as_of=NOW)
    asyncio.run(first.aclose())
    assert len(calls) == 2 and {q for _, q in calls} == {"AAPL"}

    calls.clear()
    second = _counting_scorer(calls, cache=cache)
    second.score_articles(payload, "MSFT", as_of=NOW)
    asyncio.run(second.aclose())
    assert len(calls) == 2, "a different target must not reuse the first target's scores"
    assert {q for _, q in calls} == {"MSFT"}

    calls.clear()
    third = _counting_scorer(calls, cache=cache)
    third.score_articles(payload, "AAPL", as_of=NOW)
    asyncio.run(third.aclose())
    assert calls == [], "the original target is now served entirely from cache"


# ---- re-aggregation --------------------------------------------------------------------


def test_aggregation_only_change_makes_no_scorer_calls():
    """Criterion 3: new aggregation settings reuse stored article scores."""
    calls: list = []
    scored = _counting_scorer(calls, cache=False).score_articles(_payload(), "AAPL", as_of=NOW)
    assert len(calls) == 2

    calls.clear()
    saved = json.loads(json.dumps(scored.to_dict()))  # through JSON, as a caller would store it
    again = NewsScorer(score_fn=lambda a, q: 1 / 0, cache=False).aggregate_scored(
        saved["articles"], saved["query"], as_of=saved["until"]
    )
    assert calls == [], "re-aggregation must not call the scorer"
    assert again.n_articles == scored.n_articles
    assert again.score == pytest.approx(scored.score)


def test_half_life_changes_the_aggregate_without_rescoring():
    old = make_article("AAPL beats estimates", hours_ago=96)
    new = make_article("AAPL guidance cut", hours_ago=1)
    base = NewsScorer(score_fn="keyword", cache=False).score_articles(
        [old.to_dict(), new.to_dict()], "AAPL", as_of=NOW
    )
    saved = base.to_dict()

    slow = NewsScorer(score_fn="keyword", cache=False, half_life_hours=240).aggregate_scored(
        saved["articles"], "AAPL", as_of=saved["until"]
    )
    fast = NewsScorer(score_fn="keyword", cache=False, half_life_hours=2).aggregate_scored(
        saved["articles"], "AAPL", as_of=saved["until"]
    )
    assert fast.score < slow.score, "a short half-life must lean on the recent bearish item"
    assert fast.n_articles == slow.n_articles == 2


def test_aggregate_scored_round_trips_through_scored_article():
    result = NewsScorer(score_fn="keyword", cache=False).score_articles(_payload(), "AAPL", as_of=NOW)
    rebuilt = [ScoredArticle.from_dict(d) for d in result.to_dict()["articles"]]
    assert all(isinstance(r.article, Article) for r in rebuilt)
    again = NewsScorer(score_fn="keyword", cache=False).aggregate_scored(rebuilt, "AAPL", as_of=NOW)
    assert again.score == pytest.approx(result.score)
    assert again.confidence == pytest.approx(result.confidence)


# ---- CLI -------------------------------------------------------------------------------


def test_cli_and_python_agree_on_identical_input(data_dir):
    """Criterion 9: the two paths produce the same result for the same input."""
    path = data_dir / "articles.json"
    path.write_text(json.dumps(_payload()), encoding="utf-8")
    as_of = NOW.isoformat()

    in_python = NewsScorer(score_fn="keyword", cache=False).score_articles(_payload(), "AAPL", as_of=NOW)
    result = runner.invoke(
        app,
        ["score-articles", str(path), "-q", "AAPL", "--scorer", "keyword", "--no-cache",
         "--as-of", as_of, "--json"],
    )
    assert result.exit_code == 0, result.stdout
    from_cli = json.loads(result.stdout)

    assert from_cli["score"] == pytest.approx(in_python.score)
    assert from_cli["confidence"] == pytest.approx(in_python.confidence)
    assert from_cli["status"] == in_python.status
    assert from_cli["counts"] == in_python.counts.to_dict()


def test_cli_reads_stdin_and_takes_query_from_the_payload(data_dir):
    payload = json.dumps({"query": "AAPL", "as_of": NOW.isoformat(), "articles": _payload()})
    result = runner.invoke(
        app, ["score-articles", "--scorer", "keyword", "--no-cache", "--json"], input=payload
    )
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["query"] == "AAPL"


def test_cli_requires_a_target():
    result = runner.invoke(app, ["score-articles", "--scorer", "keyword"], input=json.dumps(_payload()))
    assert result.exit_code == 1
    assert "no target" in result.stderr  # diagnostics stay off stdout


def test_cli_rejects_malformed_json_and_articles(data_dir):
    bad_json = runner.invoke(app, ["score-articles", "-q", "AAPL"], input="{not json")
    assert bad_json.exit_code == 1 and "not valid JSON" in bad_json.stderr

    bad_article = runner.invoke(
        app, ["score-articles", "-q", "AAPL", "--scorer", "keyword"], input=json.dumps([{"title": "x"}])
    )
    assert bad_article.exit_code == 1 and "published" in bad_article.stderr


def test_cli_fetch_output_is_valid_score_articles_input(data_dir):
    """The documented round trip: fetch --out, then score exactly those articles."""
    path = data_dir / "news.json"
    path.write_text(json.dumps(_payload()), encoding="utf-8")  # same shape fetch --out writes
    result = runner.invoke(
        app, ["score-articles", str(path), "-q", "AAPL", "--scorer", "keyword", "--no-cache", "--json"]
    )
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["counts"]["scored"] == 2
