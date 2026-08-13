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

import json
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

            CREATE TABLE IF NOT EXISTS credits_snapshot (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                ts TEXT NOT NULL,
                snapshots TEXT NOT NULL,
                last_cost REAL DEFAULT 0,
                model TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS session_credits (
                session_id TEXT PRIMARY KEY,
                nano_aiu REAL DEFAULT 0,
                requests INTEGER DEFAULT 0,
                premium_cost REAL DEFAULT 0,
                model TEXT DEFAULT '',
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS complexity_scores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                ts TEXT NOT NULL,
                score INTEGER NOT NULL,
                tier TEXT DEFAULT 'base',
                used_fusion INTEGER DEFAULT 0,
                preset TEXT DEFAULT '',
                reason TEXT DEFAULT '',
                task TEXT DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_usage_session
                ON usage_events(session_id);
            CREATE INDEX IF NOT EXISTS idx_usage_ts
                ON usage_events(ts);
            CREATE INDEX IF NOT EXISTS idx_complexity_session
                ON complexity_scores(session_id);
            CREATE INDEX IF NOT EXISTS idx_complexity_ts
                ON complexity_scores(ts);
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

    def record_complexity(
        self,
        session_id: str,
        score: int,
        tier: str = "base",
        used_fusion: bool = False,
        preset: str = "",
        reason: str = "",
        task: str = "",
    ) -> dict[str, Any]:
        """Record a task-complexity grade (Auto Fusion).

        Args:
            session_id: Session the grade belongs to.
            score: Complexity 0-100.
            tier: 'base' or 'fusion' (the recommended/chosen tier).
            used_fusion: Whether Fusion was actually used for this task.
            preset: Fusion preset id used (if any).
            reason: One-line rationale from the grader.
            task: Short task summary (truncated).
        """
        ts = datetime.now(timezone.utc).isoformat()
        try:
            score = max(0, min(100, int(score)))
        except (TypeError, ValueError):
            score = 0
        self._conn.execute(
            """INSERT INTO complexity_scores
               (session_id, ts, score, tier, used_fusion, preset, reason, task)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (session_id, ts, score, tier, 1 if used_fusion else 0,
             preset, reason[:500], task[:1000]),
        )
        self._conn.commit()
        return {
            "session_id": session_id, "ts": ts, "score": score, "tier": tier,
            "used_fusion": used_fusion, "preset": preset, "reason": reason,
        }

    def complexity_scores(
        self, session_id: str | None = None, limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Return recent complexity grades, newest first.

        If session_id is None, returns grades across all sessions (for the
        global graph).
        """
        if session_id:
            rows = self._conn.execute(
                """SELECT session_id, ts, score, tier, used_fusion, preset, reason, task
                   FROM complexity_scores WHERE session_id = ?
                   ORDER BY ts DESC LIMIT ?""",
                (session_id, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """SELECT session_id, ts, score, tier, used_fusion, preset, reason, task
                   FROM complexity_scores ORDER BY ts DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [
            {
                "session_id": r["session_id"], "ts": r["ts"], "score": r["score"],
                "tier": r["tier"], "used_fusion": bool(r["used_fusion"]),
                "preset": r["preset"], "reason": r["reason"], "task": r["task"],
            }
            for r in rows
        ]

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

    def record_credits(
        self, snapshots: dict[str, Any], last_cost: float = 0.0, model: str = ""
    ) -> None:
        """Persist the latest account-wide premium-quota ("AI Credits") snapshot.

        The Copilot SDK reports the same account-level quota on every session's
        ``assistant.usage`` event, so we keep a single most-recent snapshot
        (id=1) rather than one per session. ``snapshots`` is the quota dict keyed
        by type (e.g. ``premium_interactions``), each value a dict with
        entitlement/used/remaining_percentage/reset_date fields.
        """
        if not snapshots:
            return
        ts = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """INSERT INTO credits_snapshot (id, ts, snapshots, last_cost, model)
               VALUES (1, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 ts=excluded.ts, snapshots=excluded.snapshots,
                 last_cost=excluded.last_cost, model=excluded.model""",
            (ts, json.dumps(snapshots), float(last_cost or 0.0), model or ""),
        )
        self._conn.commit()

    def get_credits(self) -> dict[str, Any] | None:
        """Return the latest persisted AI Credits snapshot, or None if unset."""
        row = self._conn.execute(
            "SELECT ts, snapshots, last_cost, model FROM credits_snapshot WHERE id = 1",
        ).fetchone()
        if not row:
            return None
        try:
            snapshots = json.loads(row["snapshots"])
        except (json.JSONDecodeError, TypeError):
            snapshots = {}
        return {
            "ts": row["ts"],
            "snapshots": snapshots,
            "last_cost": row["last_cost"],
            "model": row["model"],
        }

    def add_session_aiu(
        self,
        session_id: str,
        nano_aiu: float = 0.0,
        premium_cost: float = 0.0,
        model: str = "",
    ) -> dict[str, Any]:
        """Accumulate consumed AI Units ("AI Credits") for a session.

        The Copilot SDK reports per-API-call consumption as
        ``copilotUsage.totalNanoAiu`` (nano AI Units). One AI Unit equals one
        "AI Credit" in the GitHub usage-based billing model. We sum these per
        Enclave session so the web UI can show the running total (mirroring the
        Copilot CLI's "AI Credits" indicator). ``premium_cost`` is the legacy
        premium-request cost (1 per frontier-model call) kept for reference.
        """
        ts = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """INSERT INTO session_credits
                 (session_id, nano_aiu, requests, premium_cost, model, updated_at)
               VALUES (?, ?, 1, ?, ?, ?)
               ON CONFLICT(session_id) DO UPDATE SET
                 nano_aiu = nano_aiu + excluded.nano_aiu,
                 requests = requests + 1,
                 premium_cost = premium_cost + excluded.premium_cost,
                 model = excluded.model,
                 updated_at = excluded.updated_at""",
            (session_id, float(nano_aiu or 0.0), float(premium_cost or 0.0), model or "", ts),
        )
        self._conn.commit()
        return self.get_session_credits(session_id) or {}

    def get_session_credits(self, session_id: str) -> dict[str, Any] | None:
        """Return cumulative AI Credits (AIU) consumed for a session.

        ``aiu`` is the human-facing AI Credits figure (nano AIU / 1e9).
        """
        row = self._conn.execute(
            """SELECT nano_aiu, requests, premium_cost, model, updated_at
               FROM session_credits WHERE session_id = ?""",
            (session_id,),
        ).fetchone()
        if not row:
            return None
        nano = row["nano_aiu"] or 0.0
        return {
            "session_id": session_id,
            "nano_aiu": nano,
            "aiu": round(nano / 1_000_000_000, 2),
            "requests": row["requests"] or 0,
            "premium_cost": row["premium_cost"] or 0.0,
            "model": row["model"] or "",
            "updated_at": row["updated_at"],
        }

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()
