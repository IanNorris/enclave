"""Tests for the MCP server module."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from enclave.orchestrator.mcp_server import mcp


class TestMCPServerRegistration:
    """Test that the MCP server has the expected tools and resources."""

    def test_server_name(self) -> None:
        assert mcp.name == "Enclave"

    def test_tools_registered(self) -> None:
        """All expected tools should be registered."""
        # Access internal tool registry
        tools = mcp._tool_manager._tools
        expected_tools = [
            "sessions_list",
            "session_info",
            "audit_log",
            "cost_stats",
            "session_audit",
            "system_status",
        ]
        for name in expected_tools:
            assert name in tools, f"Tool '{name}' not registered"


class TestMCPTools:
    """Test MCP tool implementations."""

    def test_sessions_list_empty(self, tmp_path: Path) -> None:
        """sessions_list returns empty when no sessions exist."""
        from enclave.orchestrator.mcp_server import sessions_list
        with patch("enclave.orchestrator.mcp_server._get_sessions", return_value=[]):
            result = sessions_list()
        data = json.loads(result)
        assert data == []

    def test_sessions_list_with_data(self) -> None:
        from enclave.orchestrator.mcp_server import sessions_list
        mock_sessions = [
            {"id": "abc123", "name": "test", "status": "running",
             "profile": "dev", "created_at": "2026-01-01"},
        ]
        with patch("enclave.orchestrator.mcp_server._get_sessions", return_value=mock_sessions):
            result = sessions_list()
        data = json.loads(result)
        assert len(data) == 1
        assert data[0]["name"] == "test"

    def test_session_info_found(self) -> None:
        from enclave.orchestrator.mcp_server import session_info
        mock_sessions = [
            {"id": "abc123-full-id", "name": "test", "status": "running"},
        ]
        with patch("enclave.orchestrator.mcp_server._get_sessions", return_value=mock_sessions):
            result = session_info("abc123")
        data = json.loads(result)
        assert data["name"] == "test"

    def test_session_info_not_found(self) -> None:
        from enclave.orchestrator.mcp_server import session_info
        with patch("enclave.orchestrator.mcp_server._get_sessions", return_value=[]):
            result = session_info("nonexistent")
        data = json.loads(result)
        assert "error" in data

    def test_audit_log_tool(self, tmp_path: Path) -> None:
        from enclave.orchestrator.mcp_server import audit_log
        from enclave.common.audit import AuditLog
        audit = AuditLog(str(tmp_path))
        audit.log("test_event", session_id="s1")

        with patch("enclave.orchestrator.mcp_server._get_data_dir", return_value=str(tmp_path)):
            result = audit_log(tail=10)
        data = json.loads(result)
        assert len(data) == 1
        assert data[0]["event"] == "test_event"

    def test_cost_stats_tool(self, tmp_path: Path) -> None:
        from enclave.orchestrator.mcp_server import cost_stats
        from enclave.common.cost_tracker import CostTracker
        tracker = CostTracker(str(tmp_path))
        tracker.record_usage("s1", input_tokens=1000, output_tokens=200)
        tracker.close()

        with patch("enclave.orchestrator.mcp_server._get_data_dir", return_value=str(tmp_path)):
            result = cost_stats()
        data = json.loads(result)
        assert data["total_input_tokens"] == 1000

    def test_system_status_tool(self) -> None:
        from enclave.orchestrator.mcp_server import system_status
        with patch("enclave.orchestrator.mcp_server._get_sessions", return_value=[
            {"status": "running"}, {"status": "stopped"},
        ]):
            result = system_status()
        data = json.loads(result)
        assert data["sessions_running"] == 1
        assert data["sessions_stopped"] == 1
        assert data["sessions_total"] == 2
