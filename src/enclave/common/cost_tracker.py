"""Cost and token tracking for Enclave sessions.

Tracks LLM token usage per session and provides aggregate stats.
Data is stored in a SQLite database for efficient querying.

Usage:
    tracker = CostTracker(data_dir="/path/to/enclave/data")
    tracker.record_usage(
        session_id="abc", input_tokens=1000, output_tokens=200,
    )
    stats = tracker.session_stats("abc")
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class CostTracker:
    """Token usage tracker backed by SQLite."""

    # Approximate costs per 1M tokens (USD) for common models.
    # These are estimates — actual costs depend on the provider.
    DEFAULT_RATES: dict[str, dict[str, float]] = {
        "default": {"input": 3.0, "output": 15.0},  # per 1M tokens
    }

    def __init__(self, data_dir: str) -> None:
        db_path = Path(data_dir) / "cost_tracking.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS usage_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                ts TEXT NOT NULL,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                total_tokens INTEGER DEFAULT 0,
                model TEXT DEFAULT '',
                event_type TEXT DEFAULT 'turn'
            );

            CREATE TABLE IF NOT EXISTS budgets (
                session_id TEXT PRIMARY KEY,
                max_tokens INTEGER DEFAULT 0,
                alert_threshold REAL DEFAULT 0.8
            );

            CREATE INDEX IF NOT EXISTS idx_usage_session
                ON usage_events(session_id);
            CREATE INDEX IF NOT EXISTS idx_usage_ts
                ON usage_events(ts);
        """)
        self._conn.commit()

    def record_usage(
        self,
        session_id: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        total_tokens: int = 0,
        model: str = "",
        event_type: str = "turn",
    ) -> dict[str, Any]:
        """Record a token usage event.

        Args:
            session_id: Session that consumed tokens.
            input_tokens: Number of input/prompt tokens.
            output_tokens: Number of output/completion tokens.
            total_tokens: Total tokens (if not sum of in+out).
            model: Model identifier.
            event_type: Event type (turn, compaction, etc).

        Returns:
            The recorded event as a dict.
        """
        if total_tokens == 0:
            total_tokens = input_tokens + output_tokens

        ts = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """INSERT INTO usage_events
               (session_id, ts, input_tokens, output_tokens, total_tokens, model, event_type)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (session_id, ts, input_tokens, output_tokens, total_tokens, model, event_type),
        )
        self._conn.commit()

        return {
            "session_id": session_id,
            "ts": ts,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "model": model,
            "event_type": event_type,
        }

    def session_stats(self, session_id: str) -> dict[str, Any]:
        """Get aggregate token usage for a session.

        Returns:
            Dict with total_input, total_output, total_tokens, turn_count,
            estimated_cost_usd.
        """
        row = self._conn.execute(
            """SELECT
                COALESCE(SUM(input_tokens), 0) AS total_input,
                COALESCE(SUM(output_tokens), 0) AS total_output,
                COALESCE(SUM(total_tokens), 0) AS total_tokens,
                COUNT(*) AS turn_count
               FROM usage_events WHERE session_id = ?""",
            (session_id,),
        ).fetchone()

        total_input = row["total_input"]
        total_output = row["total_output"]
        total_tokens = row["total_tokens"]
        turn_count = row["turn_count"]

        # Estimate cost using default rates
        rates = self.DEFAULT_RATES["default"]
        cost = (total_input / 1_000_000 * rates["input"] +
                total_output / 1_000_000 * rates["output"])

        return {
            "session_id": session_id,
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "total_tokens": total_tokens,
            "turn_count": turn_count,
            "estimated_cost_usd": round(cost, 4),
        }

    def global_stats(self) -> dict[str, Any]:
        """Get aggregate usage across all sessions."""
        row = self._conn.execute(
            """SELECT
                COALESCE(SUM(input_tokens), 0) AS total_input,
                COALESCE(SUM(output_tokens), 0) AS total_output,
                COALESCE(SUM(total_tokens), 0) AS total_tokens,
                COUNT(*) AS turn_count,
                COUNT(DISTINCT session_id) AS session_count
               FROM usage_events""",
        ).fetchone()

        total_input = row["total_input"]
        total_output = row["total_output"]
        rates = self.DEFAULT_RATES["default"]
        cost = (total_input / 1_000_000 * rates["input"] +
                total_output / 1_000_000 * rates["output"])

        return {
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "total_tokens": row["total_tokens"],
            "turn_count": row["turn_count"],
            "session_count": row["session_count"],
            "estimated_cost_usd": round(cost, 4),
        }

    def set_budget(
        self, session_id: str, max_tokens: int, alert_threshold: float = 0.8
    ) -> None:
        """Set a token budget for a session.

        Args:
            session_id: Session to budget.
            max_tokens: Maximum total tokens allowed.
            alert_threshold: Fraction (0-1) at which to alert.
        """
        self._conn.execute(
            """INSERT OR REPLACE INTO budgets (session_id, max_tokens, alert_threshold)
               VALUES (?, ?, ?)""",
            (session_id, max_tokens, alert_threshold),
        )
        self._conn.commit()

    def check_budget(self, session_id: str) -> dict[str, Any] | None:
        """Check if a session is within budget.

        Returns:
            Dict with budget info and status, or None if no budget set.
        """
        budget = self._conn.execute(
            "SELECT * FROM budgets WHERE session_id = ?",
            (session_id,),
        ).fetchone()

        if not budget:
            return None

        stats = self.session_stats(session_id)
        used = stats["total_tokens"]
        max_tokens = budget["max_tokens"]
        threshold = budget["alert_threshold"]

        return {
            "session_id": session_id,
            "used_tokens": used,
            "max_tokens": max_tokens,
            "percent_used": round(used / max_tokens * 100, 1) if max_tokens else 0,
            "over_budget": used > max_tokens,
            "alert": used >= max_tokens * threshold,
        }

    def recent_usage(self, session_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """Get recent usage events for a session."""
        rows = self._conn.execute(
            """SELECT * FROM usage_events
               WHERE session_id = ?
               ORDER BY ts DESC LIMIT ?""",
            (session_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()
