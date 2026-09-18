"""Jev scorer: response conversion and the call path, with a fake SDK client."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from newsscore import JevScorer

from conftest import make_article


def fake_response(probs, confidence=0.8, relevant=0.9, category="earnings", novel=0.4, impact="high"):
    return SimpleNamespace(
        model="jev-latest",
        answers={
            "sentiment": SimpleNamespace(score=max(probs, key=probs.get), confidence=confidence, probabilities=probs),
            "relevant": SimpleNamespace(noul=relevant),
            "impact": SimpleNamespace(choice=impact, probabilities={impact: 0.8, "low": 0.2}),
            "category": SimpleNamespace(choice=category, confidence=0.7, probabilities={category: 0.7, "other": 0.3}),
            "novel": SimpleNamespace(noul=novel),
        },
    )


def test_convert_maps_levels_to_unit_interval():
    very_positive = JevScorer._convert(fake_response({0: 0, 1: 0, 2: 0, 3: 0, 4: 1.0}))
    neutral = JevScorer._convert(fake_response({2: 1.0}))
    mixed = JevScorer._convert(fake_response({0: 0.5, 4: 0.5}))
    assert very_positive.score == 1.0 and neutral.score == 0.0 and mixed.score == 0.0
    assert very_positive.confidence == 0.8 and very_positive.relevance == 0.9
    assert very_positive.labels["category"] == "earnings" and very_positive.labels["novel"] == 0.4
    assert very_positive.expected_impact == "high"
    assert very_positive.labels["impact_probs"] == {"high": 0.8, "low": 0.2}


def test_convert_ignores_an_impact_level_it_does_not_know():
    """Never invent a level: an unexpected label becomes None, not a guess."""
    out = JevScorer._convert(fake_response({4: 1.0}, impact="catastrophic"))
    assert out.expected_impact is None


def test_convert_survives_a_response_without_the_impact_question():
    stripped = fake_response({4: 1.0})
    del stripped.answers["impact"]
    assert JevScorer._convert(stripped).expected_impact is None


def test_convert_falls_back_to_point_score_without_probabilities():
    out = JevScorer._convert(fake_response({}) if False else SimpleNamespace(
        model="m",
        answers={
            "sentiment": SimpleNamespace(score=3.0, confidence=0.5, probabilities=None),
            "relevant": SimpleNamespace(noul=1.0),
            "category": SimpleNamespace(choice="other", probabilities={}),
            "novel": SimpleNamespace(noul=0.0),
        },
    ))
    assert out.score == 0.5


class FakeClient:
    def __init__(self):
        self.calls = []

    async def system_one(self, *, state, questions):
        self.calls.append((state, set(questions)))
        level = 4 if "beat" in state["title"] else 0
        return fake_response({level: 1.0})

    async def aclose(self):
        self.closed = True


def test_call_path_uses_client_and_questions():
    client = FakeClient()
    scorer = JevScorer(client=client, concurrency=2)
    arts = [make_article("Apple beats"), make_article("Apple misses")]
    out = asyncio.run(scorer(arts, "AAPL"))
    assert [s.score for s in out] == [1.0, -1.0]
    assert client.calls[0][1] == {"sentiment", "relevant", "impact", "category", "novel"}
    assert client.calls[0][0]["query"] == "AAPL"
    asyncio.run(scorer.aclose())
    assert client.closed
