"""Persistent storage for deferred (non-blocking) agent questions.

Each session gets its own SQLite database at
{workspace_base}/{session_id}/.enclave/deferred_asks.db
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class DeferredAsksStore:
    """SQLite-backed store for deferred questions from an agent session."""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS deferred_asks (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                question TEXT NOT NULL,
                choices TEXT,
                context TEXT,
                priority TEXT DEFAULT 'normal',
                tags TEXT,
                status TEXT DEFAULT 'pending',
                answer TEXT,
                created_at TEXT NOT NULL,
                answered_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_asks_status
                ON deferred_asks(status);
            CREATE INDEX IF NOT EXISTS idx_asks_session
                ON deferred_asks(session_id);
        """)

    def add(
        self,
        session_id: str,
        question: str,
        choices: list[str] | None = None,
        context: str | None = None,
        priority: str = "normal",
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Add a new deferred question. Returns the created record."""
        ask_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """INSERT INTO deferred_asks
               (id, session_id, question, choices, context, priority, tags, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
            (
                ask_id,
                session_id,
                question,
                json.dumps(choices) if choices else None,
                context,
                priority,
                json.dumps(tags) if tags else None,
                now,
            ),
        )
        self._conn.commit()
        return self._row_to_dict(self._conn.execute(
            "SELECT * FROM deferred_asks WHERE id = ?", (ask_id,)
        ).fetchone())

    def list_pending(self, session_id: str | None = None) -> list[dict[str, Any]]:
        """List pending asks, optionally filtered by session."""
        if session_id:
            rows = self._conn.execute(
                "SELECT * FROM deferred_asks WHERE status = 'pending' AND session_id = ? ORDER BY created_at DESC",
                (session_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM deferred_asks WHERE status = 'pending' ORDER BY created_at DESC"
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def list_all(self, session_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """List all asks (any status), optionally filtered by session."""
        if session_id:
            rows = self._conn.execute(
                "SELECT * FROM deferred_asks WHERE session_id = ? ORDER BY created_at DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM deferred_asks ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def answer(self, ask_id: str, answer_text: str) -> dict[str, Any] | None:
        """Mark an ask as answered. Returns the updated record or None."""
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "UPDATE deferred_asks SET status = 'answered', answer = ?, answered_at = ? WHERE id = ? AND status = 'pending'",
            (answer_text, now, ask_id),
        )
        self._conn.commit()
        row = self._conn.execute("SELECT * FROM deferred_asks WHERE id = ?", (ask_id,)).fetchone()
        return self._row_to_dict(row) if row else None

    def dismiss(self, ask_id: str) -> bool:
        """Mark an ask as dismissed. Returns True if updated."""
        now = datetime.now(timezone.utc).isoformat()
        cur = self._conn.execute(
            "UPDATE deferred_asks SET status = 'dismissed', answered_at = ? WHERE id = ? AND status = 'pending'",
            (now, ask_id),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def pending_count(self, session_id: str | None = None) -> int:
        """Count pending asks."""
        if session_id:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM deferred_asks WHERE status = 'pending' AND session_id = ?",
                (session_id,),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM deferred_asks WHERE status = 'pending'"
            ).fetchone()
        return row[0] if row else 0

    def get(self, ask_id: str) -> dict[str, Any] | None:
        """Get a single ask by ID."""
        row = self._conn.execute("SELECT * FROM deferred_asks WHERE id = ?", (ask_id,)).fetchone()
        return self._row_to_dict(row) if row else None

    def close(self) -> None:
        self._conn.close()

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        # Deserialize JSON fields
        for key in ("choices", "tags"):
            if d.get(key):
                try:
                    d[key] = json.loads(d[key])
                except (json.JSONDecodeError, TypeError):
                    pass
        return d


# Global store cache — one DB per workspace
_store_lock = threading.Lock()
_stores: dict[str, DeferredAsksStore] = {}


def get_deferred_asks_store(workspace_base: Path) -> DeferredAsksStore:
    """Get or create the global deferred asks store.

    Uses a single DB at {workspace_base}/.enclave/deferred_asks.db
    shared across all sessions (the session_id column distinguishes them).
    """
    key = str(workspace_base)
    with _store_lock:
        if key not in _stores:
            db_path = workspace_base / ".enclave" / "deferred_asks.db"
            _stores[key] = DeferredAsksStore(db_path)
        return _stores[key]
