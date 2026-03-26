"""Tests for the audit log system."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from enclave.common.audit import AuditLog


@pytest.fixture
def audit(tmp_path: Path) -> AuditLog:
    """Create an AuditLog writing to a temp directory."""
    return AuditLog(str(tmp_path))


class TestAuditLog:
    """Tests for AuditLog."""

    def test_creates_audit_directory(self, tmp_path: Path) -> None:
        audit = AuditLog(str(tmp_path))
        assert (tmp_path / "audit").is_dir()

    def test_log_global_event(self, audit: AuditLog) -> None:
        entry = audit.log("orchestrator_started")
        assert entry["event"] == "orchestrator_started"
        assert "ts" in entry
        # Verify written to global log
        lines = (audit.audit_dir / "global.jsonl").read_text().strip().split("\n")
        assert len(lines) == 1
        assert json.loads(lines[0])["event"] == "orchestrator_started"

    def test_log_session_event(self, audit: AuditLog) -> None:
        entry = audit.log(
            "session_created", session_id="abc123",
            user="@ian:matrix.local", name="my-project",
        )
        assert entry["session_id"] == "abc123"
        assert entry["user"] == "@ian:matrix.local"
        assert entry["name"] == "my-project"

        # Should appear in both global and per-session logs
        global_lines = (audit.audit_dir / "global.jsonl").read_text().strip().split("\n")
        session_lines = (audit.audit_dir / "abc123.jsonl").read_text().strip().split("\n")
        assert len(global_lines) == 1
        assert len(session_lines) == 1

    def test_log_multiple_events(self, audit: AuditLog) -> None:
        audit.log("event1", session_id="s1")
        audit.log("event2", session_id="s1")
        audit.log("event3", session_id="s2")

        global_entries = audit.read_global()
        assert len(global_entries) == 3

        s1_entries = audit.read_session("s1")
        assert len(s1_entries) == 2
        assert s1_entries[0]["event"] == "event1"
        assert s1_entries[1]["event"] == "event2"

        s2_entries = audit.read_session("s2")
        assert len(s2_entries) == 1

    def test_read_session_tail(self, audit: AuditLog) -> None:
        for i in range(10):
            audit.log(f"event_{i}", session_id="s1")

        entries = audit.read_session("s1", tail=3)
        assert len(entries) == 3
        assert entries[0]["event"] == "event_7"
        assert entries[2]["event"] == "event_9"

    def test_read_global_tail(self, audit: AuditLog) -> None:
        for i in range(10):
            audit.log(f"event_{i}")

        entries = audit.read_global(tail=5)
        assert len(entries) == 5
        assert entries[0]["event"] == "event_5"

    def test_read_nonexistent_session(self, audit: AuditLog) -> None:
        entries = audit.read_session("nonexistent")
        assert entries == []

    def test_read_empty_global(self, audit: AuditLog) -> None:
        entries = audit.read_global()
        assert entries == []

    def test_arbitrary_kwargs(self, audit: AuditLog) -> None:
        entry = audit.log(
            "permission_granted", session_id="s1",
            perm_type="filesystem", target="/etc/hosts",
            scope="session",
        )
        assert entry["perm_type"] == "filesystem"
        assert entry["target"] == "/etc/hosts"
        assert entry["scope"] == "session"

    def test_timestamps_are_utc_iso(self, audit: AuditLog) -> None:
        entry = audit.log("test")
        ts = entry["ts"]
        # Should be ISO 8601 with timezone
        assert "T" in ts
        assert ts.endswith("+00:00") or ts.endswith("Z")

    def test_concurrent_sessions_isolated(self, audit: AuditLog) -> None:
        """Multiple sessions don't interfere with each other's logs."""
        audit.log("start", session_id="a")
        audit.log("start", session_id="b")
        audit.log("tool", session_id="a", tool="bash")
        audit.log("tool", session_id="b", tool="python")

        a_entries = audit.read_session("a")
        b_entries = audit.read_session("b")
        assert len(a_entries) == 2
        assert len(b_entries) == 2
        assert a_entries[1]["tool"] == "bash"
        assert b_entries[1]["tool"] == "python"

    def test_corrupt_line_skipped(self, audit: AuditLog) -> None:
        """Corrupt JSONL lines are gracefully skipped."""
        audit.log("good", session_id="s1")
        # Manually inject a corrupt line
        log_path = audit.audit_dir / "s1.jsonl"
        with open(log_path, "a") as f:
            f.write("not valid json\n")
        audit.log("also_good", session_id="s1")

        entries = audit.read_session("s1")
        assert len(entries) == 2
        assert entries[0]["event"] == "good"
        assert entries[1]["event"] == "also_good"
