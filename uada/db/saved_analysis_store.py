"""
Saved Analysis Store (P3-4)
==============================
SQLite-backed persistence layer for saved analyses.

Each saved analysis captures a question, the SQL that was executed, the full
UADAResponse JSON, and user-supplied metadata (name, tags, connection_id).

Thread-safe: all writes use a threading.Lock; reads are lock-free.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS saved_analyses (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    question    TEXT NOT NULL,
    sql_text    TEXT,
    response    TEXT NOT NULL,   -- UADAResponse JSON
    connection_id TEXT,
    tags        TEXT,            -- JSON array of strings
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_created ON saved_analyses(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_connection ON saved_analyses(connection_id);
"""


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


class SavedAnalysisStore:
    """
    Thread-safe SQLite store for saved analyses.

    Parameters
    ----------
    db_path:
        Path to the SQLite database file.  Defaults to ``.uada_saved_analyses.db``
        in the current working directory.  Pass ``":memory:"`` for testing.
    """

    def __init__(self, db_path: str | Path = ".uada_saved_analyses.db") -> None:
        self._db_path = str(db_path)
        self._lock = threading.Lock()
        self._init_db()

    # ── Internal ──────────────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(_DDL)
            conn.commit()

    # ── Public API ────────────────────────────────────────────────────────────

    def save(
        self,
        *,
        name: str,
        question: str,
        sql_text: str | None,
        response: dict[str, Any],
        connection_id: str | None = None,
        tags: list[str] | None = None,
        analysis_id: str | None = None,
    ) -> str:
        """
        Persist a new saved analysis and return its ID.

        Parameters
        ----------
        name:
            Human-readable name (e.g. ``"Q3 Revenue by Region"``).
        question:
            The original user question.
        sql_text:
            The SQL that was executed.
        response:
            The full UADAResponse as a dict (use ``.model_dump()``).
        connection_id:
            The database connection this analysis belongs to.
        tags:
            Optional list of string tags for filtering.
        analysis_id:
            Optional caller-supplied ID; auto-generated if omitted.

        Returns
        -------
        str
            The ID of the saved analysis.
        """
        aid = analysis_id or str(uuid.uuid4())
        now = _now_iso()
        row = (
            aid,
            name,
            question,
            sql_text,
            json.dumps(response, default=str),
            connection_id,
            json.dumps(tags or []),
            now,
            now,
        )
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO saved_analyses
                  (id, name, question, sql_text, response, connection_id, tags, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                row,
            )
            conn.commit()
        logger.info("Saved analysis %s: %r", aid, name)
        return aid

    def get(self, analysis_id: str) -> dict[str, Any] | None:
        """Return a single saved analysis or None if not found."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM saved_analyses WHERE id = ?", (analysis_id,)
            ).fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    def list(
        self,
        *,
        connection_id: str | None = None,
        tag: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """
        Return saved analyses ordered by created_at DESC.

        Optionally filter by connection_id or by a single tag.
        """
        filters: list[str] = []
        params: list[Any] = []

        if connection_id is not None:
            filters.append("connection_id = ?")
            params.append(connection_id)

        where = ("WHERE " + " AND ".join(filters)) if filters else ""

        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM saved_analyses {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                params + [limit, offset],
            ).fetchall()

        results = [self._row_to_dict(r) for r in rows]

        # Tag filter (post-query, since tags stored as JSON string)
        if tag is not None:
            results = [r for r in results if tag in r.get("tags", [])]

        return results

    def delete(self, analysis_id: str) -> bool:
        """Delete a saved analysis. Returns True if a row was deleted."""
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM saved_analyses WHERE id = ?", (analysis_id,)
            )
            conn.commit()
        return cursor.rowcount > 0

    def count(self, *, connection_id: str | None = None) -> int:
        """Return the total number of saved analyses."""
        where = "WHERE connection_id = ?" if connection_id else ""
        params = [connection_id] if connection_id else []
        with self._connect() as conn:
            return conn.execute(
                f"SELECT COUNT(*) FROM saved_analyses {where}", params
            ).fetchone()[0]

    # ── Serialisation ─────────────────────────────────────────────────────────

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["tags"] = json.loads(d.get("tags") or "[]")
        # Omit the full response blob from list views — callers load it via get()
        return d
