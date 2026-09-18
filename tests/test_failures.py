"""Failure handling: per-article isolation, retries, run status and strict exits.

Covers the behaviour that makes a bad run distinguishable from a neutral one, which
matters most to callers that act on the number rather than read it.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import httpx
import pytest
from typer.testing import CliRunner

from newsscore import (
    ArticleScore,
    NewsScorer,
    NewsSource,
    RunStatus,
    ScorerUnavailable,
    ScoreItemError,
    register,
)
from newsscore.cli import EXIT_PARTIAL, EXIT_UNUSABLE, app
from newsscore.config import SourceSpec, SourceStore
from newsscore.scoring import default_scorer, make_scorer

from conftest import make_article

runner = CliRunner()


@register
class ListSource(NewsSource):
    """Serves a fixed list; registered so the CLI can load it from a saved config."""

    type_name = "listsource"
    requires_key = False
    articles: list = []

    async def fetch(self, query, since, until, client: httpx.AsyncClient) -> list:
        return list(type(self).articles)


def _scorer(fn, **kwargs):
    fn.name = kwargs.pop("name", "test-fn")
    scorer = NewsScorer(score_fn=fn, cache=kwargs.pop("cache", False), retry_backoff=0.0, **kwargs)
    return scorer


def _articles(n=6, bad="BOOM"):
    return [make_article(f"news {i}", hours_ago=i) for i in range(n - 1)] + [make_article(bad, hours_ago=n)]


# ---- per-article isolation -----------------------------------------------------------


@pytest.mark.parametrize("style", ["raises", "reports"])
def test_one_bad_article_does_not_discard_its_siblings(style):
    """Criterion 4: a single timeout keeps the rest of the batch and names the failure."""

    async def fn(articles, query):
        out = []
        for a in articles:
            if "BOOM" in a.title:
                if style == "raises":
                    raise TimeoutError("request timed out")
                out.append(ScoreItemError("request timed out", error_type="timeout"))
            else:
                out.append(0.5)
        return out

    arts = _articles(6)
    scored, errors, failures = asyncio.run(_scorer(fn, retries=1)._score(arts, "AAPL"))

    assert len(scored) == 5, "successful siblings must survive"
    assert len(failures) == 1
    assert failures[0].article_id == arts[-1].id
    assert failures[0].error_type in {"timeout", "timeout_error"}
    assert any("failed to score" in e for e in errors)


def test_failed_articles_are_not_aggregated_as_neutral():
    """A failure must not enter the mean as a 0.0 and drag the aggregate toward neutral."""

    async def fn(articles, query):
        return [
            ScoreItemError("nope", error_type="bad_request") if "BOOM" in a.title else 1.0
            for a in articles
        ]

    scorer = _scorer(fn)
    scorer.source_add(ListSource(name="s"))
    ListSource.articles = _articles(3)
    result = scorer.score("AAPL", days=30)

    assert result.score == pytest.approx(1.0), "the failed item must not pull the mean down"
    assert result.n_articles == 2 and result.counts.failed == 1


def test_successes_are_cached_even_when_a_sibling_fails(data_dir):
    """A retry must never pay twice for articles that already succeeded."""
    seen: list[str] = []

    async def fn(articles, query):
        seen.extend(a.id for a in articles)
        return [
            ScoreItemError("nope", error_type="bad_request") if "BOOM" in a.title else 0.5
            for a in articles
        ]

    arts = _articles(4)
    cache = data_dir / "c.sqlite"
    first = _scorer(fn, cache=cache, name="cached-fn")
    asyncio.run(first._score(arts, "AAPL"))
    asyncio.run(first.aclose())
    scored_ids = set(seen)

    seen.clear()
    second = _scorer(fn, cache=cache, name="cached-fn")
    scored, _, failures = asyncio.run(second._score(arts, "AAPL"))
    asyncio.run(second.aclose())

    assert len(scored) == 3 and len(failures) == 1
    assert set(seen) == {arts[-1].id}, "only the previously failed article is rescored"
    assert scored_ids > set(seen)


# ---- retries -------------------------------------------------------------------------


def test_retryable_failure_is_retried_then_recorded():
    attempts: list[int] = []

    async def fn(articles, query):
        attempts.append(len(articles))
        return [ScoreItemError("busy", error_type="rate_limit", retryable=True) for _ in articles]

    scored, _, failures = asyncio.run(_scorer(fn, retries=2)._score([make_article("a")], "AAPL"))
    assert scored == []
    assert failures[0].attempts == 3, "one initial attempt plus two retries"
    assert len(attempts) == 3


def test_retry_does_not_rescore_successful_siblings():
    batches: list[int] = []

    async def fn(articles, query):
        batches.append(len(articles))
        return [
            ScoreItemError("busy", error_type="rate_limit", retryable=True)
            if "BOOM" in a.title
            else 0.5
            for a in articles
        ]

    scored, _, failures = asyncio.run(_scorer(fn, retries=1)._score(_articles(5), "AAPL"))
    assert len(scored) == 4 and len(failures) == 1
    assert batches == [5, 1], "the retry carries only the failed article"


def test_fatal_failure_stops_the_run_instead_of_repeating_per_article():
    """Bad credentials fail identically for every article; do not ask 300 times."""
    calls: list[int] = []

    async def fn(articles, query):
        calls.append(len(articles))
        return [ScoreItemError("bad key", error_type="authentication", fatal=True) for _ in articles]

    scorer = _scorer(fn, retries=3, batch_size=2)
    scored, errors, failures = asyncio.run(scorer._score(_articles(8), "AAPL"))

    assert scored == []
    assert len(failures) == 8, "every task is still reported"
    assert any("scoring stopped" in e for e in errors)
    assert len(calls) < 4, f"fatal error should short-circuit, made {len(calls)} calls"


# ---- run status ----------------------------------------------------------------------


def _run_with(articles, fn):
    scorer = _scorer(fn)
    scorer.source_add(ListSource(name="s"))
    ListSource.articles = articles
    return scorer.score("AAPL", days=30)


def test_status_distinguishes_every_outcome():
    """Criterion 6: no-news, all-failed, partial, valid-neutral and zero-weight differ."""
    ok = lambda a, q: [0.5] * len(a)  # noqa: E731
    neutral = lambda a, q: [0.0] * len(a)  # noqa: E731
    weightless = lambda a, q: [ArticleScore(score=0.9, confidence=0.0)] * len(a)  # noqa: E731

    async def all_fail(articles, query):
        return [ScoreItemError("nope", error_type="bad_request") for _ in articles]

    async def half_fail(articles, query):
        return [
            ScoreItemError("nope", error_type="bad_request") if "BOOM" in a.title else 0.5
            for a in articles
        ]

    assert _run_with([], ok).status == RunStatus.NO_ARTICLES
    assert _run_with(_articles(3), all_fail).status == RunStatus.ALL_FAILED
    assert _run_with(_articles(3), half_fail).status == RunStatus.PARTIAL

    valid_neutral = _run_with(_articles(3), neutral)
    zero_weight = _run_with(_articles(3), weightless)
    assert valid_neutral.status == RunStatus.OK and valid_neutral.score == 0.0
    assert zero_weight.status == RunStatus.NO_WEIGHT and zero_weight.score == 0.0
    assert valid_neutral.status != zero_weight.status, "0.0 means different things here"
    assert valid_neutral.ok and not zero_weight.ok


def test_counts_reconcile():
    dupe = make_article("same headline", source="a")
    twin = make_article("Same   Headline!", source="b")

    async def half_fail(articles, query):
        return [
            ScoreItemError("nope", error_type="bad_request") if "BOOM" in a.title else 0.5
            for a in articles
        ]

    result = _run_with([*_articles(4), dupe, twin], half_fail)
    c = result.counts
    assert c.fetched == c.deduplicated + c.submitted
    assert c.submitted == c.scored + c.failed
    assert c.deduplicated == 1


def test_failures_survive_serialisation():
    async def fn(articles, query):
        return [ScoreItemError("nope", error_type="bad_request") for _ in articles]

    data = _run_with(_articles(2), fn).to_dict()
    assert data["status"] == RunStatus.ALL_FAILED
    assert data["counts"]["failed"] == 2
    assert data["failures"][0]["error_type"] == "bad_request"


# ---- strict selection ----------------------------------------------------------------


def test_explicit_jev_without_credentials_does_not_fall_back(monkeypatch):
    """Criterion 5: asking for Jev by name must fail, not silently score with keywords."""
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)

    with pytest.raises(ScorerUnavailable):
        make_scorer("jev")
    with pytest.raises(ScorerUnavailable):
        NewsScorer(score_fn="jev", cache=False)

    # the automatic default is still allowed to fall back, because nobody asked for Jev
    assert type(default_scorer()).__name__ == "KeywordScorer"


# ---- CLI ------------------------------------------------------------------------------


def _save_source(articles):
    ListSource.articles = articles
    SourceStore().add(SourceSpec(type="listsource", name="s"), replace=True)


def test_cli_default_stays_backward_compatible():
    # words the keyword scorer actually recognises, so the run carries real weight
    _save_source([make_article("AAPL beats estimates"), make_article("AAPL upgraded", hours_ago=1)])
    result = runner.invoke(app, ["score", "AAPL", "--scorer", "keyword", "--no-cache", "-d", "30"])
    assert result.exit_code == 0
    assert "status      ok" in result.stdout


@pytest.mark.parametrize(
    "fail_on,expected",
    [("none", 0), ("unusable", EXIT_UNUSABLE), ("partial", EXIT_UNUSABLE)],
)
def test_cli_strict_exit_codes_for_unusable_runs(fail_on, expected):
    """Criterion 10: nonzero status, with the diagnostics still written."""
    _save_source([])
    result = runner.invoke(
        app, ["score", "ZZZ", "--scorer", "keyword", "--no-cache", "--fail-on", fail_on]
    )
    assert result.exit_code == expected
    assert "no_articles" in result.stdout


def test_cli_strict_json_still_emits_machine_readable_output():
    """A failing strict run must not cost the caller its diagnostics."""
    import json

    _save_source([])
    result = runner.invoke(
        app,
        ["score", "ZZZ", "--scorer", "keyword", "--no-cache", "--json", "--fail-on", "unusable"],
    )
    assert result.exit_code == EXIT_UNUSABLE
    payload = json.loads(result.stdout)
    assert payload["status"] == RunStatus.NO_ARTICLES
    assert payload["counts"]["submitted"] == 0


def test_cli_rejects_unknown_fail_on():
    result = runner.invoke(app, ["score", "AAPL", "--fail-on", "sometimes"])
    assert result.exit_code == 1
