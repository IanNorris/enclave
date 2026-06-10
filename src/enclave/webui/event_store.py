"""Per-session event store for the Web UI.

Persists all agent events (tool calls, thinking, responses, file sends, etc.)
to a SQLite database in the session workspace so the Web UI can display full
event history even after page reloads or WebSocket disconnects.

Each session gets its own SQLite database at:
    {workspace_base}/{session_id}/.enclave/events.db
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("enclave.event_store")

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);
CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp);
"""


class EventStore:
    """SQLite-backed event store for a single session."""

    def __init__(self, db_path: Path):
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        # Initialize schema eagerly
        conn = self._conn()
        conn.executescript(_CREATE_SQL)
        conn.commit()

    def _conn(self) -> sqlite3.Connection:
        """Get or create a thread-local connection."""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(
                str(self._db_path),
                check_same_thread=False,
                timeout=5.0,
            )
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn.row_factory = sqlite3.Row
        return self._local.conn

    def append(self, event_type: str, data: dict[str, Any] | None = None) -> int:
        """Append an event and return its id."""
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        conn = self._conn()
        cur = conn.execute(
            "INSERT INTO events (type, timestamp, data) VALUES (?, ?, ?)",
            (event_type, ts, json.dumps(data or {})),
        )
        conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def get_events(
        self,
        *,
        since_id: int | None = None,
        since_timestamp: str | None = None,
        types: list[str] | None = None,
        level: str = "full",
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Query events with optional filtering.

        level:
            "full" — all events
            "major" — only response, file_send, ask_user, turn_start, turn_end
        """
        conditions: list[str] = []
        params: list[Any] = []

        if since_id is not None:
            conditions.append("id > ?")
            params.append(since_id)
        if since_timestamp:
            conditions.append("timestamp > ?")
            params.append(since_timestamp)
        if types:
            placeholders = ",".join("?" * len(types))
            conditions.append(f"type IN ({placeholders})")
            params.extend(types)
        if level == "major":
            major_types = ("response", "file_send", "ask_user", "ask_user_response", "turn_start", "turn_end")
            placeholders = ",".join("?" * len(major_types))
            conditions.append(f"type IN ({placeholders})")
            params.extend(major_types)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        query = f"SELECT id, type, timestamp, data FROM events {where} ORDER BY id ASC LIMIT ?"
        params.append(limit)

        conn = self._conn()
        rows = conn.execute(query, params).fetchall()
        return [
            {
                "id": r["id"],
                "type": r["type"],
                "timestamp": r["timestamp"],
                "data": json.loads(r["data"]),
            }
            for r in rows
        ]

    def close(self) -> None:
        """Close the connection."""
        if hasattr(self._local, "conn") and self._local.conn:
            self._local.conn.close()
            self._local.conn = None


# Cache of open event stores (session_id → EventStore)
_stores: dict[str, EventStore] = {}
_stores_lock = threading.Lock()


def get_event_store(workspace_base: Path, session_id: str) -> EventStore:
    """Get or create an EventStore for a session."""
    with _stores_lock:
        if session_id not in _stores:
            db_path = workspace_base / session_id / ".enclave" / "events.db"
            _stores[session_id] = EventStore(db_path)
        return _stores[session_id]


# Event types worth persisting (major outputs + tool lifecycle).
# Excludes streaming deltas, thinking tokens, activity, ping/turn markers.
PERSIST_TYPES = frozenset({
    "tool_start", "tool_complete", "response", "file_send", "ask_user",
    "user_message", "structured_response",
})


def persist_event(
    workspace_base: Path,
    session_id: str,
    event: dict[str, Any],
    *,
    swallow: bool = True,
) -> bool:
    """Persist a control-socket event to the session's event store.

    Only event types in PERSIST_TYPES are stored. The ``ok`` and ``type``
    keys are dropped from the stored payload (``type`` is a column).

    Returns ``True`` if the event was written, ``False`` if it was skipped
    (not a persistable type) or failed while ``swallow`` is set.

    By default failures are swallowed so persistence never disrupts the
    caller, but they are always logged — a silent drop here means permanent
    data loss with no trace, which previously made this bug class very hard
    to debug. Pass ``swallow=False`` to propagate the exception.
    """
    event_type = event.get("type", "")
    # Thinking blocks stream as start/delta/end; only the finalized "end" event
    # (full content) is persisted so reloads show reasoning in history without
    # flooding the store with per-token deltas.
    is_thinking_end = event_type == "thinking" and event.get("phase") == "end"
    if event_type not in PERSIST_TYPES and not is_thinking_end:
        return False
    try:
        store = get_event_store(workspace_base, session_id)
        data = {k: v for k, v in event.items() if k not in ("ok", "type")}
        store.append(event_type, data)
        return True
    except Exception:
        log.exception(
            "Failed to persist %s event for session %s (DATA LOSS)",
            event_type, session_id,
        )
        if not swallow:
            raise
        return False
