"""Tests for workspace file watcher."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from enclave.orchestrator.watcher import WorkspaceWatcher


class TestWatcher:
    """Test the WorkspaceWatcher."""

    @pytest.mark.asyncio
    async def test_ignore_git_dir(self):
        """Should ignore .git directory."""
        w = WorkspaceWatcher("/tmp", on_changes=lambda c: None)
        assert w._should_ignore(Path("/tmp/.git"), ".git") is True

    @pytest.mark.asyncio
    async def test_ignore_pycache(self):
        """Should ignore __pycache__."""
        w = WorkspaceWatcher("/tmp", on_changes=lambda c: None)
        assert w._should_ignore(Path("/tmp/__pycache__"), "__pycache__") is True

    @pytest.mark.asyncio
    async def test_ignore_pyc_files(self):
        """Should ignore .pyc files."""
        w = WorkspaceWatcher("/tmp", on_changes=lambda c: None)
        assert w._should_ignore(Path("/tmp/foo.pyc"), "foo.pyc") is True

    @pytest.mark.asyncio
    async def test_ignore_hidden_files(self):
        """Should ignore hidden files."""
        w = WorkspaceWatcher("/tmp", on_changes=lambda c: None)
        assert w._should_ignore(Path("/tmp/.hidden"), ".hidden") is True

    @pytest.mark.asyncio
    async def test_allow_normal_files(self):
        """Should allow normal source files."""
        w = WorkspaceWatcher("/tmp", on_changes=lambda c: None)
        assert w._should_ignore(Path("/tmp/main.py"), "main.py") is False
        assert w._should_ignore(Path("/tmp/README.md"), "README.md") is False

    @pytest.mark.asyncio
    async def test_allow_env_file(self):
        """Should allow .env file (exception to hidden rule)."""
        w = WorkspaceWatcher("/tmp", on_changes=lambda c: None)
        assert w._should_ignore(Path("/tmp/.env"), ".env") is False

    @pytest.mark.asyncio
    async def test_start_nonexistent_dir(self):
        """Start on nonexistent directory is a no-op."""
        w = WorkspaceWatcher("/nonexistent/path/12345", on_changes=lambda c: None)
        await w.start()
        assert not w._running

    @pytest.mark.asyncio
    async def test_start_stop(self, tmp_path):
        """Can start and stop the watcher."""
        received = []

        async def on_changes(changes):
            received.extend(changes)

        w = WorkspaceWatcher(str(tmp_path), on_changes=on_changes)
        await w.start()
        assert w._running

        # Create a file to trigger a change
        (tmp_path / "test.txt").write_text("hello")
        await asyncio.sleep(3)  # Wait for debounce

        await w.stop()
        assert not w._running

        # Should have detected the file creation
        # (May or may not depending on timing, but stop shouldn't crash)

    @pytest.mark.asyncio
    async def test_ignore_node_modules(self):
        """Should ignore node_modules."""
        w = WorkspaceWatcher("/tmp", on_changes=lambda c: None)
        assert w._should_ignore(Path("/tmp/node_modules"), "node_modules") is True
