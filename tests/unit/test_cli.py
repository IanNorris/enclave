"""Tests for the enclavectl CLI."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


class TestCLIMain:
    """Test the CLI entry point and commands."""

    def test_help(self):
        """CLI shows help without error."""
        result = subprocess.run(
            [sys.executable, "-m", "enclave.cli.main", "--help"],
            capture_output=True, text=True, timeout=10,
            cwd=str(Path(__file__).parent.parent.parent),
            env={**__import__("os").environ, "PYTHONPATH": str(Path(__file__).parent.parent.parent / "src")},
        )
        assert result.returncode == 0
        assert "enclavectl" in result.stdout
        assert "status" in result.stdout
        assert "sessions" in result.stdout
        assert "tui" in result.stdout

    def test_status_subcommand_exists(self):
        """Status subcommand is recognized."""
        result = subprocess.run(
            [sys.executable, "-m", "enclave.cli.main", "status", "--help"],
            capture_output=True, text=True, timeout=10,
            cwd=str(Path(__file__).parent.parent.parent),
            env={**__import__("os").environ, "PYTHONPATH": str(Path(__file__).parent.parent.parent / "src")},
        )
        assert result.returncode == 0

    def test_sessions_subcommand_exists(self):
        """Sessions subcommand is recognized."""
        result = subprocess.run(
            [sys.executable, "-m", "enclave.cli.main", "sessions", "--help"],
            capture_output=True, text=True, timeout=10,
            cwd=str(Path(__file__).parent.parent.parent),
            env={**__import__("os").environ, "PYTHONPATH": str(Path(__file__).parent.parent.parent / "src")},
        )
        assert result.returncode == 0

    def test_cleanup_subcommand_exists(self):
        """Cleanup subcommand is recognized."""
        result = subprocess.run(
            [sys.executable, "-m", "enclave.cli.main", "cleanup", "--help"],
            capture_output=True, text=True, timeout=10,
            cwd=str(Path(__file__).parent.parent.parent),
            env={**__import__("os").environ, "PYTHONPATH": str(Path(__file__).parent.parent.parent / "src")},
        )
        assert result.returncode == 0


class TestCLIFunctions:
    """Test CLI helper functions."""

    def test_get_sessions_missing_file(self, tmp_path):
        """Returns empty list when sessions file doesn't exist."""
        from enclave.cli.main import _get_sessions

        config = MagicMock()
        config.container.session_base = str(tmp_path / "nonexistent")
        assert _get_sessions(config) == []

    def test_get_sessions_valid(self, tmp_path):
        """Reads sessions from JSON file."""
        from enclave.cli.main import _get_sessions

        sessions_file = tmp_path / "sessions.json"
        sessions_file.write_text(json.dumps([
            {"id": "test-1", "name": "Test", "status": "running"},
            {"id": "test-2", "name": "Test2", "status": "stopped"},
        ]))

        config = MagicMock()
        config.container.session_base = str(tmp_path)
        result = _get_sessions(config)
        assert len(result) == 2
        assert result[0]["id"] == "test-1"

    def test_get_sessions_invalid_json(self, tmp_path):
        """Returns empty list for corrupt JSON."""
        from enclave.cli.main import _get_sessions

        sessions_file = tmp_path / "sessions.json"
        sessions_file.write_text("not json")

        config = MagicMock()
        config.container.session_base = str(tmp_path)
        assert _get_sessions(config) == []

    def test_workspace_size(self, tmp_path):
        """Gets workspace disk usage."""
        from enclave.cli.main import _workspace_size

        (tmp_path / "test.txt").write_text("hello world")
        size = _workspace_size(str(tmp_path))
        assert size != "?"

    def test_workspace_size_missing(self):
        """Returns ? for missing workspace."""
        from enclave.cli.main import _workspace_size
        assert _workspace_size("/nonexistent/path/12345") == "?"
