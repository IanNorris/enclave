"""Structured audit logging for Enclave.

Writes append-only JSONL audit trails per session (and a global log).
Each line is a self-contained JSON object with event type, timestamp,
session context, and event-specific data.

Usage:
    audit = AuditLog(data_dir="/path/to/enclave/data")
    audit.log("session_created", session_id="abc", name="my-project", profile="dev")
    audit.log("permission_granted", session_id="abc", perm_type="filesystem", target="/etc")
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class AuditLog:
    """Append-only JSONL audit logger.

    Writes to:
      - {data_dir}/audit/global.jsonl     — all events
      - {data_dir}/audit/{session_id}.jsonl — per-session events
    """

    def __init__(self, data_dir: str) -> None:
        self._audit_dir = Path(data_dir) / "audit"
        self._audit_dir.mkdir(parents=True, exist_ok=True)
        self._global_path = self._audit_dir / "global.jsonl"

    def log(
        self,
        event: str,
        *,
        session_id: str = "",
        user: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Write an audit event.

        Args:
            event: Event type (e.g. "session_created", "permission_granted").
            session_id: Associated session ID (empty for global events).
            user: Matrix user ID or display name of the actor.
            **kwargs: Arbitrary event-specific data.

        Returns:
            The full audit entry dict (for testing).
        """
        entry: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
        }
        if session_id:
            entry["session_id"] = session_id
        if user:
            entry["user"] = user
        entry.update(kwargs)

        line = json.dumps(entry, default=str, separators=(",", ":"))

        # Append to global log
        with open(self._global_path, "a") as f:
            f.write(line + "\n")

        # Append to per-session log if applicable
        if session_id:
            session_path = self._audit_dir / f"{session_id}.jsonl"
            with open(session_path, "a") as f:
                f.write(line + "\n")

        return entry

    def read_session(self, session_id: str, tail: int = 100) -> list[dict[str, Any]]:
        """Read recent audit entries for a session.

        Args:
            session_id: Session to read logs for.
            tail: Number of most recent entries to return.

        Returns:
            List of audit entry dicts, most recent last.
        """
        path = self._audit_dir / f"{session_id}.jsonl"
        if not path.exists():
            return []

        lines = path.read_text().strip().split("\n")
        entries = []
        for line in lines[-tail:]:
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return entries

    def read_global(self, tail: int = 100) -> list[dict[str, Any]]:
        """Read recent entries from the global audit log.

        Args:
            tail: Number of most recent entries to return.

        Returns:
            List of audit entry dicts, most recent last.
        """
        if not self._global_path.exists():
            return []

        lines = self._global_path.read_text().strip().split("\n")
        entries = []
        for line in lines[-tail:]:
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return entries

    @property
    def audit_dir(self) -> Path:
        """Return the audit directory path."""
        return self._audit_dir
