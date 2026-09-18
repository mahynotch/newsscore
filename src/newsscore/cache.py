"""SQLite cache of per-article scores.

Scoring is the expensive step (Jev calls cost money, user models cost time), so
every :class:`~newsscore.ArticleScore` is stored under
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

from .models import Article, ArticleScore, make_id

# v2 keys on a full identity rather than (scorer, query, article_id), so a changed
# rubric, model or headline cannot serve an answer computed under the old contract.
# Rows written by 0.1 live on in the old `scores` table and simply never match;
# they were produced by a different scoring contract, so reusing them would be wrong.
# `newsscore cache-clear` removes both.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS scores_v2 (
    identity   TEXT PRIMARY KEY,
    scorer     TEXT NOT NULL,
    query      TEXT NOT NULL,
    article_id TEXT NOT NULL,
    payload    TEXT NOT NULL,
    created    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS scores_v2_scorer ON scores_v2 (scorer);
"""


def identity(scorer: str, fingerprint: str, query: str, article: "Article") -> str:
    """The cache key: who scored it, under what contract, for whom, and what they read."""
    return make_id(scorer, fingerprint, query, article.id, article.content_digest)


def default_cache_path() -> Path:
    override = os.environ.get("NEWSSCORE_CACHE")
    if override:
        return Path(override).expanduser()
    return Path(user_cache_dir("newsscore")) / "scores.sqlite"


class ScoreCache:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else default_cache_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def get_many(
        self, scorer: str, fingerprint: str, query: str, articles: Iterable["Article"]
    ) -> dict[str, ArticleScore]:
        """Cached scores by article id, for articles whose identity still matches."""
        wanted = {identity(scorer, fingerprint, query, a): a.id for a in articles}
        if not wanted:
            return {}
        keys = list(wanted)
        found: dict[str, ArticleScore] = {}
        # SQLite caps bound parameters; chunk to stay well under the limit.
        for start in range(0, len(keys), 500):
            chunk = keys[start : start + 500]
            marks = ",".join("?" * len(chunk))
            rows = self._conn.execute(
                f"SELECT identity, payload FROM scores_v2 WHERE identity IN ({marks})", chunk
            )
            for key, payload in rows:
                found[wanted[key]] = ArticleScore.from_dict(json.loads(payload))
        return found

    def put_many(
        self, scorer: str, fingerprint: str, query: str, items: Mapping[str, "Article"],
        scores: Mapping[str, ArticleScore],
    ) -> None:
        if not scores:
            return
        now = time.time()
        rows = [
            (
                identity(scorer, fingerprint, query, items[article_id]),
                scorer,
                query,
                article_id,
                json.dumps(score.to_dict(), default=str),
                now,
            )
            for article_id, score in scores.items()
            if article_id in items
        ]
        self._conn.executemany(
            "INSERT OR REPLACE INTO scores_v2 (identity, scorer, query, article_id, payload, created)"
            " VALUES (?,?,?,?,?,?)",
            rows,
        )
        self._conn.commit()

    def clear(self, scorer: str | None = None) -> int:
        removed = 0
        for table in ("scores_v2", "scores"):
            if not self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone():
                continue
            cur = (
                self._conn.execute(f"DELETE FROM {table} WHERE scorer=?", (scorer,))
                if scorer
                else self._conn.execute(f"DELETE FROM {table}")
            )
            removed += max(0, cur.rowcount)
        self._conn.commit()
        return removed

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "ScoreCache":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
