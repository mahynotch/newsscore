"""The .env loader and source store."""

from __future__ import annotations

import os

from jev_sentiment import NewsScorer, load_env
from jev_sentiment.config import SourceSpec, SourceStore, parse_env


def test_parse_env_forms():
    text = """
    # comment
    export A=1
    B = "two words"
    C='x'
    D=plain # trailing comment
    E=
    F=            # blank value with a comment
    =nokey
    """
    assert parse_env(text) == {"A": "1", "B": "two words", "C": "x", "D": "plain", "E": "", "F": ""}


def test_load_env_respects_existing_and_skips_blank(data_dir, monkeypatch):
    env = data_dir / ".env"
    env.write_text("FINNHUB_API_KEY=from-file\nTIINGO_API_KEY=\nPOLYGON_API_KEY=file\n", encoding="utf-8")
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    monkeypatch.delenv("TIINGO_API_KEY", raising=False)
    monkeypatch.setenv("POLYGON_API_KEY", "real")

    assert load_env(env) == env
    assert os.environ["FINNHUB_API_KEY"] == "from-file"
    assert "TIINGO_API_KEY" not in os.environ
    assert os.environ["POLYGON_API_KEY"] == "real"
    assert load_env(data_dir / "missing.env") is None
    monkeypatch.delenv("FINNHUB_API_KEY")


def test_from_config_uses_env_file_for_keys(data_dir, monkeypatch):
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    env = data_dir / ".env"
    env.write_text("FINNHUB_API_KEY=abc\n", encoding="utf-8")
    SourceStore().add(SourceSpec(type="finnhub", name="fh"))  # no key stored
    scorer = NewsScorer.from_config(score_fn="keyword", cache=False, env_file=env)
    assert scorer.sources["fh"].api_key == "abc"
    monkeypatch.delenv("FINNHUB_API_KEY")


def test_jevsent_env_override_is_first_candidate(data_dir, monkeypatch):
    env = data_dir / "custom.env"
    env.write_text("MARKETAUX_API_KEY=m\n", encoding="utf-8")
    monkeypatch.setenv("JEVSENT_ENV", str(env))
    monkeypatch.delenv("MARKETAUX_API_KEY", raising=False)
    assert load_env() == env
    assert os.environ["MARKETAUX_API_KEY"] == "m"
    monkeypatch.delenv("MARKETAUX_API_KEY")
