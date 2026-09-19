"""Reaching Jev through OpenRouter's Decisions router instead of TypeSafe."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from newsscore import JevScorer
from newsscore.scoring.jev import DEFAULT_MODELS, classify_error
from newsscore.scoring.openrouter import (
    DECISIONS_URL,
    OpenRouterDecisions,
    OpenRouterError,
    to_questions,
)
from newsscore.scoring.protocol import ScorerUnavailable

from conftest import make_article

KEY = "or-test-key"


def decisions_body(*, sentiment=3.4, probs=None, impact=None, model="typesafe/jev-1.13"):
    """A Decisions response in the shape OpenRouter's OpenAPI schema documents."""
    answers = {
        "sentiment": {
            "type": "score",
            "score": sentiment,
            "confidence": 0.91,
            "probabilities": probs if probs is not None else {"0": 0.0, "1": 0.0, "2": 0.1, "3": 0.9, "4": 0.0},
        },
        "relevant": {"type": "noul", "noul": 0.87},
        "category": {
            "type": "choice",
            "choice": "earnings",
            "confidence": 0.7,
            "probabilities": {"earnings": 0.8, "other": 0.2},
        },
        "novel": {"type": "noul", "noul": 0.55},
    }
    if impact:
        answers["impact"] = {
            "type": "choice",
            "choice": impact,
            "probabilities": {impact: 0.9, "low": 0.1},
        }
    return {
        "id": "gen-abc123",
        "model": model,
        "provider": "TypeSafe",
        "usage": {"input_tokens": 1000, "output_tokens": 120, "cost": 0.000042},
        "answers": answers,
    }


def transport(handler):
    """An OpenRouterDecisions whose HTTP layer is a local handler."""
    client = OpenRouterDecisions(api_key=KEY)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


# ---- the wire format --------------------------------------------------------------


def test_request_matches_the_documented_decisions_schema():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=decisions_body())

    scorer = JevScorer(api_key=KEY, provider="openrouter", client=transport(handler))
    asyncio.run(scorer([make_article("AAPL beats estimates")], "AAPL"))

    assert seen["url"] == DECISIONS_URL
    assert seen["auth"] == f"Bearer {KEY}"
    body = seen["body"]
    assert set(body) == {"model", "state", "questions"}, "the three required fields, nothing stray"
    assert body["model"] == DEFAULT_MODELS["openrouter"]
    assert body["state"]["query"] == "AAPL"

    q = body["questions"]
    assert {name: item["type"] for name, item in q.items()} == {
        "sentiment": "score", "relevant": "noul", "category": "choice", "novel": "noul"
    }
    assert isinstance(q["sentiment"]["criteria"], list), "score criteria are ordered levels"
    assert isinstance(q["category"]["criteria"], dict), "choice criteria are label -> description"
    assert "criteria" not in q["relevant"], "noul needs no criteria"


def test_both_providers_ask_exactly_the_same_questions():
    """The two routes must never drift into asking subtly different things."""
    ts = JevScorer(api_key="k", provider="typesafe", impact=True)
    orr = JevScorer(api_key="k", provider="openrouter", impact=True)
    assert ts._question_set() == orr._question_set()


def test_the_provider_is_part_of_the_cache_identity():
    """Same version string from two resellers is a claim, not a fact."""
    ts = JevScorer(api_key="k", provider="typesafe")
    orr = JevScorer(api_key="k", provider="openrouter")
    assert ts.fingerprint and orr.fingerprint
    assert ts.fingerprint != orr.fingerprint


def test_an_alias_still_refuses_to_cache():
    """OpenRouter spells a floating alias with a leading ~; it must not be cached.

    The pinned ids are `typesafe/jev-1.13` and the dated `typesafe/jev-1.13-20260917`;
    `~typesafe/jev-latest` moves, so answers filed under it could come from anywhere.
    """
    alias = JevScorer(api_key="k", provider="openrouter", model="~typesafe/jev-latest")
    assert alias.fingerprint is None
    for pinned in ("typesafe/jev-1.13", "typesafe/jev-1.13-20260917"):
        assert JevScorer(api_key="k", provider="openrouter", model=pinned).fingerprint

    dated = JevScorer(api_key="k", provider="openrouter", model="typesafe/jev-1.13-20260917")
    floating = JevScorer(api_key="k", provider="openrouter", model="typesafe/jev-1.13")
    assert dated.fingerprint != floating.fingerprint, "different ids, different cache"


# ---- conversion -------------------------------------------------------------------


def test_answers_convert_exactly_as_the_sdk_path_does():
    scorer = JevScorer(
        api_key=KEY, provider="openrouter", impact=True,
        client=transport(lambda r: httpx.Response(200, json=decisions_body(impact="high"))),
    )
    [out] = asyncio.run(scorer([make_article("AAPL beats estimates")], "AAPL"))
    # levels 2 and 3 at 0.1/0.9 -> expected level 2.9, rescaled from [0,4] to [-1,1]
    assert out.score == pytest.approx(2.9 / 4 * 2 - 1)
    assert out.confidence == pytest.approx(0.91)
    assert out.relevance == pytest.approx(0.87)
    assert out.expected_impact == "high"
    assert out.labels["category"] == "earnings"
    assert out.labels["novel"] == pytest.approx(0.55)
    assert out.labels["sentiment_probs"] == {0: 0.0, 1: 0.0, 2: 0.1, 3: 0.9, 4: 0.0}
    assert out.labels["usage"] == {"input_tokens": 1000, "output_tokens": 120}
    assert out.labels["request_id"] == "gen-abc123"
    assert out.labels["model"] == "typesafe/jev-1.13"


def test_score_probabilities_keyed_by_level_text_are_mapped_back_to_indices():
    """The router may render a score distribution by label rather than position."""
    body = decisions_body(probs={"bad": 0.0, "poor": 0.0, "flat": 0.2, "good": 0.8, "great": 0.0})
    body["answers"]["sentiment"]["legend"] = {
        "bad": "", "poor": "", "flat": "", "good": "", "great": ""
    }
    scorer = JevScorer(api_key=KEY, provider="openrouter",
                       client=transport(lambda r: httpx.Response(200, json=body)))
    [out] = asyncio.run(scorer([make_article("AAPL beats")], "AAPL"))
    assert out.labels["sentiment_probs"] == {0: 0.0, 1: 0.0, 2: 0.2, 3: 0.8, 4: 0.0}


# ---- failures ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,retryable,fatal",
    [(429, True, False), (503, True, False), (500, True, False),
     (401, False, True), (402, False, True), (404, False, True),
     (400, False, False), (422, False, False)],
)
def test_http_status_becomes_a_retry_verdict(status, retryable, fatal):
    scorer = JevScorer(
        api_key=KEY, provider="openrouter",
        client=transport(lambda r: httpx.Response(status, json={"error": {"message": "nope"}})),
    )
    [out] = asyncio.run(scorer([make_article("AAPL beats")], "AAPL"))
    assert out.retryable is retryable and out.fatal is fatal
    assert "nope" in str(out), "the provider's own message survives"


def test_a_network_failure_is_retryable():
    def boom(request):
        raise httpx.ConnectError("no route to host")

    scorer = JevScorer(api_key=KEY, provider="openrouter", client=transport(boom))
    [out] = asyncio.run(scorer([make_article("AAPL beats")], "AAPL"))
    assert out.retryable and not out.fatal


def test_classify_error_trusts_a_transport_that_states_its_verdict():
    assert classify_error(OpenRouterError("x", status=429)).retryable
    assert classify_error(OpenRouterError("x", status=401)).fatal
    assert not classify_error(OpenRouterError("x", status=400)).retryable


# ---- wiring -----------------------------------------------------------------------


def test_openrouter_needs_no_sdk_but_does_need_a_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(ScorerUnavailable, match="OPENROUTER_API_KEY"):
        JevScorer(provider="openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    assert JevScorer(provider="openrouter").provider == "openrouter"


def test_provider_defaults_to_whichever_key_is_present(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    assert JevScorer().provider == "openrouter"
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts")
    assert JevScorer().provider == "typesafe", "an existing TypeSafe setup is not hijacked"


def test_an_unknown_provider_is_rejected():
    with pytest.raises(ValueError, match="provider must be one of"):
        JevScorer(api_key="k", provider="anthropic")


def test_to_questions_drops_criteria_only_where_the_schema_allows():
    out = to_questions({
        "a": {"type": "noul", "instructions": "i"},
        "b": {"type": "score", "instructions": "i", "criteria": ["x", "y"]},
        "c": {"type": "choice", "instructions": "i", "criteria": {"k": "v"}},
    })
    assert out["a"] == {"type": "noul", "instructions": "i"}
    assert out["b"]["criteria"] == ["x", "y"]
    assert out["c"]["criteria"] == {"k": "v"}


def test_a_rejected_model_id_stops_the_run_instead_of_repeating():
    """The model id is the same for every article, so one rejection settles it."""
    body = {"error": {"message": "Model typesafe/jev-nope does not exist", "code": 400}}
    scorer = JevScorer(api_key=KEY, provider="openrouter",
                       client=transport(lambda r: httpx.Response(400, json=body)))
    [out] = asyncio.run(scorer([make_article("AAPL beats")], "AAPL"))
    assert out.fatal, "a bad model id applies to every article"

    other = {"error": {"message": "state exceeds the context window", "code": 400}}
    scorer = JevScorer(api_key=KEY, provider="openrouter",
                       client=transport(lambda r: httpx.Response(400, json=other)))
    [out] = asyncio.run(scorer([make_article("AAPL beats")], "AAPL"))
    assert not out.fatal, "an ordinary 400 still fails only its own article"
