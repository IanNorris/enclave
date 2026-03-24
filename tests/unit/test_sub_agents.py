"""Tests for sub-agent manager and search isolation."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from enclave.common.protocol import Message, MessageType
from enclave.orchestrator.sub_agents import SubAgent, SubAgentManager


@pytest.fixture
def sub_mgr():
    """Create a sub-agent manager with mocked dependencies."""
    send_message = AsyncMock(return_value="$thread-root-event")
    create_container = AsyncMock(return_value="sub-session-123")
    create_ipc_socket = AsyncMock(return_value="/tmp/test.sock")
    send_to_agent = AsyncMock(return_value=True)

    mgr = SubAgentManager(
        send_message=send_message,
        create_container=create_container,
        create_ipc_socket=create_ipc_socket,
        send_to_agent=send_to_agent,
    )
    return mgr, send_message, create_container, create_ipc_socket, send_to_agent


class TestSpawn:
    @pytest.mark.asyncio
    async def test_spawn_creates_thread_and_container(self, sub_mgr) -> None:
        mgr, send_msg, create_ctr, _, _ = sub_mgr

        sub = await mgr.spawn(
            parent_session_id="parent-1",
            room_id="!room:test",
            name="Research",
            purpose="Find auth patterns",
        )

        assert sub is not None
        assert sub.status == "running"
        assert sub.parent_session_id == "parent-1"
        assert sub.thread_event_id == "$thread-root-event"
        # Thread root message created
        send_msg.assert_called_once()
        # Container created
        create_ctr.assert_called_once()

    @pytest.mark.asyncio
    async def test_spawn_sends_initial_message(self, sub_mgr) -> None:
        mgr, _, _, _, send_to = sub_mgr

        sub = await mgr.spawn(
            parent_session_id="p1",
            room_id="!r:t",
            name="Test",
            purpose="Do something",
        )

        # Initial purpose sent to sub-agent
        send_to.assert_called_once()
        msg = send_to.call_args[0][1]
        assert msg.type == MessageType.USER_MESSAGE
        assert "Do something" in msg.payload["content"]

    @pytest.mark.asyncio
    async def test_spawn_with_network(self, sub_mgr) -> None:
        mgr, _, create_ctr, _, _ = sub_mgr

        await mgr.spawn(
            parent_session_id="p1",
            room_id="!r:t",
            name="Search",
            purpose="Search web",
            has_network=True,
            has_workspace=False,
        )

        call_kwargs = create_ctr.call_args[1]
        assert call_kwargs["has_network"] is True
        assert call_kwargs["has_workspace"] is False

    @pytest.mark.asyncio
    async def test_spawn_fails_on_thread_creation(self, sub_mgr) -> None:
        mgr, send_msg, _, _, _ = sub_mgr
        send_msg.return_value = None

        sub = await mgr.spawn(
            parent_session_id="p1",
            room_id="!r:t",
            name="Fail",
            purpose="This should fail",
        )

        assert sub is None

    @pytest.mark.asyncio
    async def test_spawn_fails_on_container_creation(self, sub_mgr) -> None:
        mgr, _, create_ctr, _, _ = sub_mgr
        create_ctr.return_value = None

        sub = await mgr.spawn(
            parent_session_id="p1",
            room_id="!r:t",
            name="Fail",
            purpose="Container fails",
        )

        assert sub is not None
        assert sub.status == "failed"


class TestComplete:
    @pytest.mark.asyncio
    async def test_complete_sends_result(self, sub_mgr) -> None:
        mgr, send_msg, _, _, send_to = sub_mgr

        sub = await mgr.spawn("p1", "!r:t", "Test", "Do stuff")
        send_msg.reset_mock()
        send_to.reset_mock()

        await mgr.complete(sub.id, "Here are the results")

        assert sub.status == "completed"
        assert sub.result == "Here are the results"
        # Completion posted to thread
        assert send_msg.call_count == 1
        # Result sent to parent
        assert send_to.call_count == 1
        parent_msg = send_to.call_args[0][1]
        assert "results" in parent_msg.payload["content"]

    @pytest.mark.asyncio
    async def test_complete_unknown_id(self, sub_mgr) -> None:
        mgr, _, _, _, _ = sub_mgr
        await mgr.complete("nonexistent", "result")
        # Should not raise


class TestFail:
    @pytest.mark.asyncio
    async def test_fail_posts_error(self, sub_mgr) -> None:
        mgr, send_msg, _, _, _ = sub_mgr

        sub = await mgr.spawn("p1", "!r:t", "Test", "Do stuff")
        send_msg.reset_mock()

        await mgr.fail(sub.id, "Out of memory")

        assert sub.status == "failed"
        send_msg.assert_called_once()
        assert "failed" in send_msg.call_args[0][1].lower()


class TestLookup:
    @pytest.mark.asyncio
    async def test_get_by_session(self, sub_mgr) -> None:
        mgr, _, _, _, _ = sub_mgr
        sub = await mgr.spawn("p1", "!r:t", "Test", "Do stuff")

        found = mgr.get_by_session(sub.session_id)
        assert found is not None
        assert found.id == sub.id

    @pytest.mark.asyncio
    async def test_get_parent_session(self, sub_mgr) -> None:
        mgr, _, _, _, _ = sub_mgr
        sub = await mgr.spawn("p1", "!r:t", "Test", "Do stuff")

        parent = mgr.get_parent_session(sub.session_id)
        assert parent == "p1"

    @pytest.mark.asyncio
    async def test_list_sub_agents(self, sub_mgr) -> None:
        mgr, _, _, _, _ = sub_mgr
        await mgr.spawn("p1", "!r:t", "A", "Task A")
        await mgr.spawn("p1", "!r:t", "B", "Task B")
        await mgr.spawn("p2", "!r2:t", "C", "Task C")

        all_subs = mgr.list_sub_agents()
        assert len(all_subs) == 3

        p1_subs = mgr.list_sub_agents(parent_session_id="p1")
        assert len(p1_subs) == 2

    @pytest.mark.asyncio
    async def test_active_count(self, sub_mgr) -> None:
        mgr, _, _, _, _ = sub_mgr
        await mgr.spawn("p1", "!r:t", "A", "Task A")
        await mgr.spawn("p1", "!r:t", "B", "Task B")

        assert mgr.active_count("p1") == 2
        assert mgr.active_count("p2") == 0


class TestSearchIsolation:
    """Test search isolation characteristics."""

    @pytest.mark.asyncio
    async def test_search_agent_has_network(self, sub_mgr) -> None:
        """Search agents get network access."""
        from enclave.orchestrator.search import search

        mgr, _, create_ctr, _, _ = sub_mgr
        await search(mgr, "p1", "!r:t", "What is Rust?")

        call_kwargs = create_ctr.call_args[1]
        assert call_kwargs["has_network"] is True

    @pytest.mark.asyncio
    async def test_search_agent_no_workspace(self, sub_mgr) -> None:
        """Search agents don't get workspace access."""
        from enclave.orchestrator.search import search

        mgr, _, create_ctr, _, _ = sub_mgr
        await search(mgr, "p1", "!r:t", "What is Rust?")

        call_kwargs = create_ctr.call_args[1]
        assert call_kwargs["has_workspace"] is False

    def test_search_system_prompt(self) -> None:
        from enclave.orchestrator.search import SEARCH_SYSTEM_PROMPT
        assert "plain text" in SEARCH_SYSTEM_PROMPT.lower()
        assert "ignore" in SEARCH_SYSTEM_PROMPT.lower()
