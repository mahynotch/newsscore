"""SQLite cache of per-article scores.

Scoring is the expensive step (Jev calls cost money, user models cost time), so
every :class:`~jev_sentiment.ArticleScore` is stored under
``(scorer_name, query, article_id)``. Re-running a query only scores articles that
have not been seen before, and back-tests can replay from the cache for free.

The cache is synchronous on purpose: SQLite calls here take microseconds and a
threadpool hop would cost more than it saves.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Iterable, Mapping

from platformdirs import user_cache_dir

from .models import ArticleScore

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scores (
    scorer     TEXT NOT NULL,
    query      TEXT NOT NULL,
    article_id TEXT NOT NULL,
    payload    TEXT NOT NULL,
    created    REAL NOT NULL,
    PRIMARY KEY (scorer, query, article_id)
)
"""


def default_cache_path() -> Path:
    override = os.environ.get("JEVSENT_CACHE")
    if override:
        return Path(override).expanduser()
    return Path(user_cache_dir("jev_sentiment")) / "scores.sqlite"


class ScoreCache:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else default_cache_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    def get_many(self, scorer: str, query: str, ids: Iterable[str]) -> dict[str, ArticleScore]:
        ids = list(ids)
        if not ids:
            return {}
        found: dict[str, ArticleScore] = {}
        # SQLite caps bound parameters; chunk to stay well under the limit.
        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            marks = ",".join("?" * len(chunk))
            rows = self._conn.execute(
                f"SELECT article_id, payload FROM scores WHERE scorer=? AND query=? AND article_id IN ({marks})",
                (scorer, query, *chunk),
            )
            for article_id, payload in rows:
                found[article_id] = ArticleScore.from_dict(json.loads(payload))
        return found

    def put_many(self, scorer: str, query: str, items: Mapping[str, ArticleScore]) -> None:
        if not items:
            return
        now = time.time()
        self._conn.executemany(
            "INSERT OR REPLACE INTO scores (scorer, query, article_id, payload, created) VALUES (?,?,?,?,?)",
            [
                (scorer, query, article_id, json.dumps(score.to_dict(), default=str), now)
                for article_id, score in items.items()
            ],
        )
        self._conn.commit()

    def clear(self, scorer: str | None = None) -> int:
        cur = (
            self._conn.execute("DELETE FROM scores WHERE scorer=?", (scorer,))
            if scorer
            else self._conn.execute("DELETE FROM scores")
        )
        self._conn.commit()
        return cur.rowcount

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "ScoreCache":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
