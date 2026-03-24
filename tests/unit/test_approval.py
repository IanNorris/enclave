"""Tests for the approval flow."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from enclave.orchestrator.approval import (
    EMOJI_DENY,
    EMOJI_ONCE,
    EMOJI_PROJECT,
    EMOJI_SESSION,
    EMOJI_PATTERN,
    ApprovalManager,
    format_approval_message,
    suggest_pattern,
)
from enclave.orchestrator.permissions import (
    PermissionDB,
    PermissionScope,
    PermissionType,
    RequestStatus,
)


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory() as tmpdir:
        pdb = PermissionDB(Path(tmpdir) / "test.db")
        yield pdb
        pdb.close()


@pytest.fixture
def approval(db):
    send_msg = AsyncMock(return_value="$event-123")
    send_react = AsyncMock(return_value="$react-1")
    mgr = ApprovalManager(
        permission_db=db,
        send_message=send_msg,
        send_reaction=send_react,
        approval_room_id="!approvals:test",
        timeout=5.0,
    )
    return mgr, send_msg, send_react


# ------------------------------------------------------------------
# Pattern suggestion
# ------------------------------------------------------------------


class TestSuggestPattern:
    def test_directory_path(self) -> None:
        assert suggest_pattern("/home/user/projects/myapp") == r"/home/user/projects/.*"

    def test_file_path(self) -> None:
        assert suggest_pattern("/tmp/foo.txt") == r"/tmp/.*"

    def test_root_path(self) -> None:
        assert suggest_pattern("/myfile") == r"/.*"

    def test_single_component(self) -> None:
        result = suggest_pattern("myfile")
        assert result == "myfile"

    def test_trailing_slash(self) -> None:
        assert suggest_pattern("/home/user/") == r"/home/.*"


# ------------------------------------------------------------------
# Message formatting
# ------------------------------------------------------------------


class TestFormatMessage:
    def test_filesystem_request(self) -> None:
        plain, html = format_approval_message(
            "Test Session", PermissionType.FILESYSTEM,
            "/home/user/code", "Need source files", 42,
        )
        assert "Permission Request #42" in plain
        assert "/home/user/code" in plain
        assert "Test Session" in plain
        assert "Need source files" in plain
        assert EMOJI_ONCE in plain

    def test_network_request(self) -> None:
        plain, html = format_approval_message(
            "Test", PermissionType.NETWORK, "api.github.com", "", 1,
        )
        assert "🌐" in plain
        assert "api.github.com" in plain

    def test_html_output(self) -> None:
        plain, html = format_approval_message(
            "Test", PermissionType.FILESYSTEM, "/tmp", "", 1,
        )
        assert "<code>/tmp</code>" in html
        assert "Permission Request #1" in html


# ------------------------------------------------------------------
# Approval flow
# ------------------------------------------------------------------


class TestApprovalFlow:
    @pytest.mark.asyncio
    async def test_already_granted_returns_immediately(self, approval) -> None:
        mgr, send_msg, _ = approval
        # Pre-grant the permission
        mgr.db.add_grant(
            "s1", "myapp", PermissionType.FILESYSTEM, "/tmp",
            PermissionScope.SESSION, "@ian:test",
        )

        status, scope = await mgr.request_permission(
            "s1", "Test", "myapp", PermissionType.FILESYSTEM, "/tmp",
        )

        assert status == RequestStatus.APPROVED
        send_msg.assert_not_called()

    @pytest.mark.asyncio
    async def test_posts_to_matrix(self, approval) -> None:
        mgr, send_msg, send_react = approval

        # Run approval in background, resolve via reaction
        async def approve_after_delay():
            await asyncio.sleep(0.2)
            mgr.handle_reaction("$event-123", EMOJI_SESSION, "@ian:test")

        task = asyncio.create_task(approve_after_delay())

        status, scope = await mgr.request_permission(
            "s1", "Test", "myapp", PermissionType.FILESYSTEM, "/tmp",
        )
        await task

        send_msg.assert_called_once()
        # 5 emoji reactions seeded
        assert send_react.call_count == 5

    @pytest.mark.asyncio
    async def test_approve_once(self, approval) -> None:
        mgr, _, _ = approval

        async def approve():
            await asyncio.sleep(0.1)
            req_id, scope = mgr.handle_reaction("$event-123", EMOJI_ONCE, "@ian:test")
            assert scope == PermissionScope.ONCE

        task = asyncio.create_task(approve())
        status, _ = await mgr.request_permission(
            "s1", "Test", "p", PermissionType.FILESYSTEM, "/x",
        )
        await task
        assert status == RequestStatus.APPROVED

    @pytest.mark.asyncio
    async def test_approve_session(self, approval) -> None:
        mgr, _, _ = approval

        async def approve():
            await asyncio.sleep(0.1)
            _, scope = mgr.handle_reaction("$event-123", EMOJI_SESSION, "@ian:test")
            assert scope == PermissionScope.SESSION

        task = asyncio.create_task(approve())
        status, _ = await mgr.request_permission(
            "s1", "Test", "p", PermissionType.FILESYSTEM, "/x",
        )
        await task
        assert status == RequestStatus.APPROVED

    @pytest.mark.asyncio
    async def test_approve_project(self, approval) -> None:
        mgr, _, _ = approval

        async def approve():
            await asyncio.sleep(0.1)
            _, scope = mgr.handle_reaction("$event-123", EMOJI_PROJECT, "@ian:test")
            assert scope == PermissionScope.PROJECT

        task = asyncio.create_task(approve())
        status, _ = await mgr.request_permission(
            "s1", "Test", "p", PermissionType.FILESYSTEM, "/x",
        )
        await task
        assert status == RequestStatus.APPROVED

    @pytest.mark.asyncio
    async def test_deny(self, approval) -> None:
        mgr, _, _ = approval

        async def deny():
            await asyncio.sleep(0.1)
            req_id, scope = mgr.handle_reaction("$event-123", EMOJI_DENY, "@ian:test")
            assert scope is None

        task = asyncio.create_task(deny())
        status, _ = await mgr.request_permission(
            "s1", "Test", "p", PermissionType.FILESYSTEM, "/secret",
        )
        await task
        assert status == RequestStatus.DENIED

    @pytest.mark.asyncio
    async def test_timeout(self, approval) -> None:
        mgr, _, _ = approval
        mgr.timeout = 0.5  # Short timeout for test

        status, scope = await mgr.request_permission(
            "s1", "Test", "p", PermissionType.FILESYSTEM, "/x",
        )

        assert status == RequestStatus.EXPIRED
        assert scope is None

    @pytest.mark.asyncio
    async def test_send_fails(self, approval) -> None:
        mgr, send_msg, _ = approval
        send_msg.return_value = None  # Simulate send failure

        status, _ = await mgr.request_permission(
            "s1", "Test", "p", PermissionType.FILESYSTEM, "/x",
        )
        assert status == RequestStatus.EXPIRED


# ------------------------------------------------------------------
# Reaction handling
# ------------------------------------------------------------------


class TestHandleReaction:
    def test_unknown_event_ignored(self, approval) -> None:
        mgr, _, _ = approval
        req_id, scope = mgr.handle_reaction("$unknown", EMOJI_ONCE, "@u:t")
        assert req_id is None
        assert scope is None

    def test_unknown_emoji_ignored(self, approval) -> None:
        mgr, _, _ = approval
        mgr._pending["$ev"] = 1
        req_id, scope = mgr.handle_reaction("$ev", "🤔", "@u:t")
        assert req_id is None
