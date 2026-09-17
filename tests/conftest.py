from __future__ import annotations

import os
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from jev_sentiment.models import Article, make_id

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)

# Where test artefacts (config files, score caches, live smoke-test output) are kept.
# Override with JEVSENT_TEST_DATA. Falls back to pytest's tmp_path if the drive is missing.
TEST_DATA_DIR = Path(os.environ.get("JEVSENT_TEST_DATA", r"E:\test_data\jev_sentiment"))


@pytest.fixture
def data_dir(request, tmp_path) -> Path:
    """A clean per-test directory under TEST_DATA_DIR (or tmp_path when unavailable)."""
    if not TEST_DATA_DIR.anchor or not Path(TEST_DATA_DIR.anchor).exists():
        return tmp_path
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", request.node.nodeid)
    path = TEST_DATA_DIR / "pytest" / safe
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture(autouse=True)
def isolated_user_dirs(data_dir, monkeypatch):
    """Keep tests away from the real config and cache files."""
    monkeypatch.setenv("JEVSENT_CONFIG", str(data_dir / "sources.json"))
    monkeypatch.setenv("JEVSENT_CACHE", str(data_dir / "scores.sqlite"))
    monkeypatch.setenv("JEVSENT_ENV", str(data_dir / "absent.env"))  # ignore the project's real .env
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)


def make_article(title: str, *, source: str = "test", hours_ago: float = 0, summary: str | None = None, url: str | None = None) -> Article:
    published = NOW - timedelta(hours=hours_ago)
    return Article(
        id=make_id(source, url or f"{title}|{published.isoformat()}"),
        source=source,
        title=title,
        published=published,
        url=url,
        summary=summary,
        symbols=("AAPL",),
    )
