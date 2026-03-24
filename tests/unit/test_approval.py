"""Tests for the approval flow."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from enclave.orchestrator.approval import (
    ANSWER_APPROVE_ONCE,
    ANSWER_APPROVE_PROJECT,
    ANSWER_APPROVE_PATTERN,
    ANSWER_CUSTOM_PATTERN,
    ANSWER_DENY_ONCE,
    ANSWER_DENY_PROJECT,
    ApprovalManager,
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
    send_msg = AsyncMock(return_value="$msg-123")
    send_react = AsyncMock(return_value="$react-1")
    send_poll = AsyncMock(return_value="$poll-123")
    end_poll = AsyncMock(return_value="$end-1")
    mgr = ApprovalManager(
        permission_db=db,
        send_message=send_msg,
        send_reaction=send_react,
        send_poll=send_poll,
        end_poll=end_poll,
        timeout=5.0,
    )
    return mgr, send_msg, send_react, send_poll


# ------------------------------------------------------------------
# Pattern suggestion
# ------------------------------------------------------------------


class TestSuggestPattern:
    def test_command_pattern(self) -> None:
        assert suggest_pattern("apt-get install -y cowsay") == r"^apt\-get\s+"

    def test_directory_path(self) -> None:
        assert suggest_pattern("/home/user/projects/myapp") == r"/home/user/projects/.*"

    def test_file_path(self) -> None:
        assert suggest_pattern("/tmp/foo.txt") == r"/tmp/.*"

    def test_root_path(self) -> None:
        assert suggest_pattern("/myfile") == r"/.*"

    def test_single_component(self) -> None:
        result = suggest_pattern("myfile")
        assert result == r"^myfile\s+"


# ------------------------------------------------------------------
# Approval flow
# ------------------------------------------------------------------


class TestApprovalFlow:
    @pytest.mark.asyncio
    async def test_already_granted_returns_immediately(self, approval) -> None:
        mgr, send_msg, _, _ = approval
        mgr.db.add_grant(
            "s1", "myapp", PermissionType.FILESYSTEM, "/tmp",
            PermissionScope.SESSION, "@ian:test",
        )

        status, scope, pattern = await mgr.request_permission(
            "s1", "Test", "myapp", PermissionType.FILESYSTEM, "/tmp",
            room_id="!room:test",
        )

        assert status == RequestStatus.APPROVED
        send_msg.assert_not_called()

    @pytest.mark.asyncio
    async def test_posts_poll(self, approval) -> None:
        mgr, _, _, send_poll = approval

        async def approve_after_delay():
            await asyncio.sleep(0.2)
            mgr.handle_poll_response(
                "$poll-123", [ANSWER_APPROVE_ONCE], "@ian:test", "!room:test"
            )

        task = asyncio.create_task(approve_after_delay())

        status, scope, pattern = await mgr.request_permission(
            "s1", "Test", "myapp", PermissionType.FILESYSTEM, "/tmp",
            room_id="!room:test",
        )
        await task

        send_poll.assert_called_once()
        assert status == RequestStatus.APPROVED

    @pytest.mark.asyncio
    async def test_approve_once(self, approval) -> None:
        mgr, _, _, _ = approval

        async def approve():
            await asyncio.sleep(0.1)
            req_id, scope, needs = mgr.handle_poll_response(
                "$poll-123", [ANSWER_APPROVE_ONCE], "@ian:test", "!room:test"
            )
            assert scope == PermissionScope.ONCE

        task = asyncio.create_task(approve())
        status, scope, _ = await mgr.request_permission(
            "s1", "Test", "p", PermissionType.FILESYSTEM, "/x",
            room_id="!room:test",
        )
        await task
        assert status == RequestStatus.APPROVED

    @pytest.mark.asyncio
    async def test_approve_project(self, approval) -> None:
        mgr, _, _, _ = approval

        async def approve():
            await asyncio.sleep(0.1)
            _, scope, _ = mgr.handle_poll_response(
                "$poll-123", [ANSWER_APPROVE_PROJECT], "@ian:test", "!room:test"
            )
            assert scope == PermissionScope.PROJECT

        task = asyncio.create_task(approve())
        status, _, _ = await mgr.request_permission(
            "s1", "Test", "p", PermissionType.FILESYSTEM, "/x",
            room_id="!room:test",
        )
        await task
        assert status == RequestStatus.APPROVED

    @pytest.mark.asyncio
    async def test_approve_pattern(self, approval) -> None:
        mgr, _, _, _ = approval

        async def approve():
            await asyncio.sleep(0.1)
            _, scope, _ = mgr.handle_poll_response(
                "$poll-123", [ANSWER_APPROVE_PATTERN], "@ian:test", "!room:test"
            )
            assert scope == PermissionScope.PATTERN

        task = asyncio.create_task(approve())
        status, scope, pattern = await mgr.request_permission(
            "s1", "Test", "p", PermissionType.PRIVILEGE, "apt install foo",
            room_id="!room:test",
        )
        await task
        assert status == RequestStatus.APPROVED
        assert scope == PermissionScope.PATTERN
        assert pattern is not None

    @pytest.mark.asyncio
    async def test_deny_once(self, approval) -> None:
        mgr, _, _, _ = approval

        async def deny():
            await asyncio.sleep(0.1)
            req_id, scope, _ = mgr.handle_poll_response(
                "$poll-123", [ANSWER_DENY_ONCE], "@ian:test", "!room:test"
            )
            assert scope is None

        task = asyncio.create_task(deny())
        status, _, _ = await mgr.request_permission(
            "s1", "Test", "p", PermissionType.FILESYSTEM, "/secret",
            room_id="!room:test",
        )
        await task
        assert status == RequestStatus.DENIED

    @pytest.mark.asyncio
    async def test_timeout(self, approval) -> None:
        mgr, _, _, _ = approval
        mgr.timeout = 0.5

        status, scope, _ = await mgr.request_permission(
            "s1", "Test", "p", PermissionType.FILESYSTEM, "/x",
            room_id="!room:test",
        )
        assert status == RequestStatus.EXPIRED
        assert scope is None

    @pytest.mark.asyncio
    async def test_send_fails(self, approval) -> None:
        mgr, _, _, send_poll = approval
        send_poll.return_value = None

        status, _, _ = await mgr.request_permission(
            "s1", "Test", "p", PermissionType.FILESYSTEM, "/x",
            room_id="!room:test",
        )
        assert status == RequestStatus.EXPIRED

    @pytest.mark.asyncio
    async def test_custom_pattern(self, approval) -> None:
        mgr, _, _, _ = approval

        async def custom_flow():
            await asyncio.sleep(0.1)
            req_id, scope, needs = mgr.handle_poll_response(
                "$poll-123", [ANSWER_CUSTOM_PATTERN], "@ian:test", "!room:test"
            )
            assert needs is True
            await asyncio.sleep(0.1)
            mgr.handle_custom_pattern(req_id, "^apt\\s+", "@ian:test")

        task = asyncio.create_task(custom_flow())
        status, scope, pattern = await mgr.request_permission(
            "s1", "Test", "p", PermissionType.PRIVILEGE, "apt install foo",
            room_id="!room:test",
        )
        await task
        assert status == RequestStatus.APPROVED
        assert scope == PermissionScope.PATTERN
        assert pattern == "^apt\\s+"


# ------------------------------------------------------------------
# Poll response handling
# ------------------------------------------------------------------


class TestHandlePollResponse:
    def test_unknown_poll_ignored(self, approval) -> None:
        mgr, _, _, _ = approval
        req_id, scope, needs = mgr.handle_poll_response(
            "$unknown", [ANSWER_APPROVE_ONCE], "@u:t", "!r:t"
        )
        assert req_id is None

    def test_empty_answers_ignored(self, approval) -> None:
        mgr, _, _, _ = approval
        mgr._pending["$ev"] = (1, "^test")
        req_id, scope, needs = mgr.handle_poll_response("$ev", [], "@u:t", "!r:t")
        assert req_id is None
