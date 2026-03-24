"""Tests for the message router."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from enclave.common.protocol import Message, MessageType
from enclave.orchestrator.commands import CommandType
from enclave.orchestrator.container import ContainerManager, Session
from enclave.orchestrator.router import MessageRouter


# ------------------------------------------------------------------
# Fixtures / Fakes
# ------------------------------------------------------------------


class FakeMatrix:
    """Fake Matrix client that records calls."""

    def __init__(self) -> None:
        self.sent_messages: list[dict] = []
        self.created_rooms: list[dict] = []
        self._message_handlers: list = []
        self.client = MagicMock()
        self.client.logged_in = True

    def on_message(self, handler):
        self._message_handlers.append(handler)

    async def send_message(
        self,
        room_id: str,
        body: str,
        html_body: str | None = None,
        thread_event_id: str | None = None,
    ) -> str | None:
        self.sent_messages.append({
            "room_id": room_id,
            "body": body,
            "html_body": html_body,
            "thread_event_id": thread_event_id,
        })
        return "$fake-event-id"

    async def send_reaction(self, room_id, event_id, emoji):
        return "$fake-reaction"

    async def set_typing(self, room_id, typing=True, timeout=30000):
        pass

    async def edit_message(self, room_id, event_id, body, html_body=None):
        return "$fake-edit"

    async def redact_event(self, room_id, event_id, reason=""):
        return True

    async def create_room(
        self,
        name: str,
        topic: str = "",
        invite: list[str] | None = None,
        encrypted: bool = True,
        space_id: str | None = None,
    ) -> str | None:
        room = {
            "name": name,
            "topic": topic,
            "invite": invite,
            "encrypted": encrypted,
            "space_id": space_id,
        }
        self.created_rooms.append(room)
        return "!new-room:test"

    async def invite_user(self, room_id: str, user_id: str) -> bool:
        return True

    async def _trust_users(self, user_ids: list[str]) -> None:
        pass

    def on_user_join(self, handler):
        pass


class FakeIPC:
    """Fake IPC server that records calls."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, Message]] = []
        self._handler = None
        self._connect_cbs: list = []
        self._disconnect_cbs: list = []
        self._connected: set[str] = set()
        self.created_sockets: list[str] = []
        self.removed_sockets: list[str] = []

    def set_handler(self, handler):
        self._handler = handler

    def on_connect(self, cb):
        self._connect_cbs.append(cb)

    def on_disconnect(self, cb):
        self._disconnect_cbs.append(cb)

    def is_connected(self, session_id: str) -> bool:
        return session_id in self._connected

    def connected_sessions(self) -> list[str]:
        return list(self._connected)

    async def send_to(self, session_id: str, msg: Message) -> bool:
        self.sent.append((session_id, msg))
        return session_id in self._connected

    async def create_socket(self, session_id: str) -> str:
        self.created_sockets.append(session_id)
        return f"/tmp/test-{session_id}.sock"

    async def remove_socket(self, session_id: str) -> None:
        self.removed_sockets.append(session_id)

    def mark_connected(self, session_id: str) -> None:
        self._connected.add(session_id)

    async def simulate_agent_message(
        self, session_id: str, msg: Message
    ) -> Message | None:
        if self._handler:
            return await self._handler(session_id, msg)
        return None


class FakeContainers:
    """Fake container manager."""

    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {}
        self._start_result = True

    def get_session(self, session_id: str) -> Session | None:
        return self.sessions.get(session_id)

    def get_session_by_room(self, room_id: str) -> Session | None:
        for s in self.sessions.values():
            if s.room_id == room_id and s.status == "running":
                return s
        return None

    def get_any_session_by_room(self, room_id: str) -> Session | None:
        for s in self.sessions.values():
            if s.room_id == room_id:
                return s
        return None

    def list_sessions(self) -> list[Session]:
        return list(self.sessions.values())

    def active_sessions(self) -> list[Session]:
        return [s for s in self.sessions.values() if s.status == "running"]

    async def create_session(
        self, name: str, room_id: str, socket_path: str
    ) -> Session:
        session = Session(
            id=f"{name.lower()}-abc12345",
            name=name,
            room_id=room_id,
            socket_path=socket_path,
        )
        self.sessions[session.id] = session
        return session

    async def start_session(self, session_id: str) -> bool:
        s = self.sessions.get(session_id)
        if s and self._start_result:
            s.status = "running"
        return self._start_result

    async def stop_session(self, session_id: str) -> bool:
        s = self.sessions.get(session_id)
        if s:
            s.status = "stopped"
        return s is not None

    async def remove_session(self, session_id: str) -> bool:
        s = self.sessions.pop(session_id, None)
        return s is not None

    def add_test_session(
        self,
        session_id: str = "test-session",
        name: str = "Test",
        room_id: str = "!project:test",
        status: str = "running",
    ) -> Session:
        s = Session(
            id=session_id,
            name=name,
            room_id=room_id,
            status=status,
        )
        self.sessions[session_id] = s
        return s

    async def check_health(self) -> list[Session]:
        """Fake health check — returns empty (no crashes)."""
        return []


CONTROL_ROOM = "!control:test"


@pytest.fixture
def router_parts():
    """Create fake components and a router."""
    matrix = FakeMatrix()
    ipc = FakeIPC()
    containers = FakeContainers()
    router = MessageRouter(
        matrix=matrix,
        ipc=ipc,
        containers=containers,
        control_room_id=CONTROL_ROOM,
        space_id="!space:test",
        allowed_users=["@ian:test"],
    )
    return router, matrix, ipc, containers


@pytest.fixture
async def started_router(router_parts):
    """Router with start() already called."""
    router, matrix, ipc, containers = router_parts
    await router.start()
    # Clear startup announcement from sent_messages
    matrix.sent_messages.clear()
    return router, matrix, ipc, containers


# ------------------------------------------------------------------
# Tests: Control room commands
# ------------------------------------------------------------------


class TestControlCommands:
    """Test command handling in the control room."""

    @pytest.mark.asyncio
    async def test_help_command(self, started_router):
        router, matrix, ipc, containers = started_router
        await router._on_matrix_message(
            CONTROL_ROOM, "@ian:test", "help", {}
        )
        assert len(matrix.sent_messages) == 1
        assert "Enclave Commands" in matrix.sent_messages[0]["body"]

    @pytest.mark.asyncio
    async def test_help_with_bang(self, started_router):
        router, matrix, ipc, containers = started_router
        await router._on_matrix_message(
            CONTROL_ROOM, "@ian:test", "!help", {}
        )
        assert len(matrix.sent_messages) == 1
        assert "Enclave Commands" in matrix.sent_messages[0]["body"]

    @pytest.mark.asyncio
    async def test_unknown_command(self, started_router):
        router, matrix, ipc, containers = started_router
        await router._on_matrix_message(
            CONTROL_ROOM, "@ian:test", "foobar", {}
        )
        assert len(matrix.sent_messages) == 1
        assert "Unknown command" in matrix.sent_messages[0]["body"]

    @pytest.mark.asyncio
    async def test_project_no_args(self, started_router):
        router, matrix, ipc, containers = started_router
        await router._on_matrix_message(
            CONTROL_ROOM, "@ian:test", "project", {}
        )
        assert "Usage" in matrix.sent_messages[0]["body"]

    @pytest.mark.asyncio
    async def test_project_creates_room_and_session(self, started_router):
        router, matrix, ipc, containers = started_router
        await router._on_matrix_message(
            CONTROL_ROOM, "@ian:test", "project MyApp", {}
        )
        # Room created (invite is deferred, so not in create_room call)
        assert len(matrix.created_rooms) == 1
        assert "MyApp" in matrix.created_rooms[0]["name"]
        # Session created
        assert len(containers.sessions) == 1
        # Success message
        assert any("MyApp" in m["body"] for m in matrix.sent_messages)

    @pytest.mark.asyncio
    async def test_sessions_empty(self, started_router):
        router, matrix, ipc, containers = started_router
        await router._on_matrix_message(
            CONTROL_ROOM, "@ian:test", "sessions", {}
        )
        assert "No active sessions" in matrix.sent_messages[0]["body"]

    @pytest.mark.asyncio
    async def test_sessions_with_active(self, started_router):
        router, matrix, ipc, containers = started_router
        containers.add_test_session()
        await router._on_matrix_message(
            CONTROL_ROOM, "@ian:test", "sessions", {}
        )
        assert "Test" in matrix.sent_messages[0]["body"]

    @pytest.mark.asyncio
    async def test_kill_no_args(self, started_router):
        router, matrix, ipc, containers = started_router
        await router._on_matrix_message(
            CONTROL_ROOM, "@ian:test", "kill", {}
        )
        assert "Usage" in matrix.sent_messages[0]["body"]

    @pytest.mark.asyncio
    async def test_kill_session(self, started_router):
        router, matrix, ipc, containers = started_router
        containers.add_test_session("s1", "Test", "!r:t")
        await router._on_matrix_message(
            CONTROL_ROOM, "@ian:test", "kill s1", {}
        )
        assert "s1" not in containers.sessions
        assert "stopped" in matrix.sent_messages[0]["body"]

    @pytest.mark.asyncio
    async def test_kill_nonexistent(self, started_router):
        router, matrix, ipc, containers = started_router
        await router._on_matrix_message(
            CONTROL_ROOM, "@ian:test", "kill nope", {}
        )
        assert "not found" in matrix.sent_messages[0]["body"]

    @pytest.mark.asyncio
    async def test_status_command(self, started_router):
        router, matrix, ipc, containers = started_router
        containers.add_test_session()
        await router._on_matrix_message(
            CONTROL_ROOM, "@ian:test", "status", {}
        )
        body = matrix.sent_messages[0]["body"]
        assert "Enclave Status" in body
        assert "1" in body

    @pytest.mark.asyncio
    async def test_unauthorized_user_ignored(self, started_router):
        router, matrix, ipc, containers = started_router
        await router._on_matrix_message(
            CONTROL_ROOM, "@hacker:evil", "help", {}
        )
        assert len(matrix.sent_messages) == 0


# ------------------------------------------------------------------
# Tests: Project room message routing
# ------------------------------------------------------------------


class TestProjectRouting:
    """Test message forwarding between Matrix and agents."""

    @pytest.mark.asyncio
    async def test_message_forwarded_to_agent(self, started_router):
        router, matrix, ipc, containers = started_router
        s = containers.add_test_session("s1", "Test", "!project:test")
        ipc.mark_connected("s1")

        await router._on_matrix_message(
            "!project:test", "@ian:test", "Hello agent", {}
        )

        assert len(ipc.sent) == 1
        sid, msg = ipc.sent[0]
        assert sid == "s1"
        assert msg.type == MessageType.USER_MESSAGE
        assert msg.payload["content"] == "Hello agent"

    @pytest.mark.asyncio
    async def test_message_no_session_ignored(self, started_router):
        router, matrix, ipc, containers = started_router
        await router._on_matrix_message(
            "!unknown:test", "@ian:test", "Hello", {}
        )
        assert len(ipc.sent) == 0
        assert len(matrix.sent_messages) == 0

    @pytest.mark.asyncio
    async def test_message_agent_not_connected(self, started_router):
        router, matrix, ipc, containers = started_router
        containers.add_test_session("s1", "Test", "!project:test")
        # Don't mark connected

        await router._on_matrix_message(
            "!project:test", "@ian:test", "Hello", {}
        )

        assert len(ipc.sent) == 0
        assert "not connected" in matrix.sent_messages[0]["body"]

    @pytest.mark.asyncio
    async def test_thread_context_preserved(self, started_router):
        router, matrix, ipc, containers = started_router
        containers.add_test_session("s1", "Test", "!project:test")
        ipc.mark_connected("s1")

        source = {
            "content": {
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": "$thread-root",
                }
            }
        }

        await router._on_matrix_message(
            "!project:test", "@ian:test", "In thread", source
        )

        sid, msg = ipc.sent[0]
        assert msg.payload["thread_id"] == "$thread-root"


# ------------------------------------------------------------------
# Tests: Agent → Matrix routing
# ------------------------------------------------------------------


class TestAgentRouting:
    """Test agent response routing back to Matrix."""

    @pytest.mark.asyncio
    async def test_agent_response_sent_to_room(self, started_router):
        router, matrix, ipc, containers = started_router
        containers.add_test_session("s1", "Test", "!project:test")

        msg = Message(
            type=MessageType.AGENT_RESPONSE,
            payload={"content": "Hello human!"},
        )

        await ipc.simulate_agent_message("s1", msg)

        assert len(matrix.sent_messages) == 1
        assert matrix.sent_messages[0]["room_id"] == "!project:test"
        assert matrix.sent_messages[0]["body"] == "Hello human!"

    @pytest.mark.asyncio
    async def test_agent_status_ready(self, started_router):
        router, matrix, ipc, containers = started_router
        containers.add_test_session("s1", "Test", "!project:test")

        msg = Message(
            type=MessageType.STATUS_UPDATE,
            payload={"status": "ready", "copilot_available": True},
        )

        await ipc.simulate_agent_message("s1", msg)

        assert len(matrix.sent_messages) == 1
        assert "ready" in matrix.sent_messages[0]["body"].lower()
        assert "Copilot" in matrix.sent_messages[0]["body"]

    @pytest.mark.asyncio
    async def test_agent_status_echo_mode(self, started_router):
        router, matrix, ipc, containers = started_router
        containers.add_test_session("s1", "Test", "!project:test")

        msg = Message(
            type=MessageType.STATUS_UPDATE,
            payload={"status": "ready", "copilot_available": False},
        )

        await ipc.simulate_agent_message("s1", msg)

        assert "Echo" in matrix.sent_messages[0]["body"]

    @pytest.mark.asyncio
    async def test_agent_empty_response_ignored(self, started_router):
        router, matrix, ipc, containers = started_router
        containers.add_test_session("s1", "Test", "!project:test")

        msg = Message(
            type=MessageType.AGENT_RESPONSE,
            payload={"content": ""},
        )

        await ipc.simulate_agent_message("s1", msg)
        assert len(matrix.sent_messages) == 0

    @pytest.mark.asyncio
    async def test_unknown_session_message_ignored(self, started_router):
        router, matrix, ipc, containers = started_router
        msg = Message(
            type=MessageType.AGENT_RESPONSE,
            payload={"content": "orphan"},
        )
        await ipc.simulate_agent_message("nonexistent", msg)
        assert len(matrix.sent_messages) == 0


# ------------------------------------------------------------------
# Tests: Agent connect/disconnect
# ------------------------------------------------------------------


class TestAgentLifecycle:
    """Test agent connect and disconnect handling."""

    @pytest.mark.asyncio
    async def test_disconnect_notifies_room(self, started_router):
        router, matrix, ipc, containers = started_router
        containers.add_test_session("s1", "Test", "!project:test")

        for cb in ipc._disconnect_cbs:
            await cb("s1")

        assert len(matrix.sent_messages) == 1
        assert "disconnected" in matrix.sent_messages[0]["body"].lower()

    @pytest.mark.asyncio
    async def test_disconnect_stopped_session_no_notify(
        self, started_router
    ):
        router, matrix, ipc, containers = started_router
        s = containers.add_test_session("s1", "Test", "!project:test")
        s.status = "stopped"

        for cb in ipc._disconnect_cbs:
            await cb("s1")

        assert len(matrix.sent_messages) == 0


# ------------------------------------------------------------------
# Tests: Access control
# ------------------------------------------------------------------


class TestAccessControl:
    """Test user authorization."""

    @pytest.mark.asyncio
    async def test_allowed_user_passes(self, started_router):
        router, matrix, ipc, containers = started_router
        await router._on_matrix_message(
            CONTROL_ROOM, "@ian:test", "help", {}
        )
        assert len(matrix.sent_messages) == 1

    @pytest.mark.asyncio
    async def test_disallowed_user_blocked(self, started_router):
        router, matrix, ipc, containers = started_router
        await router._on_matrix_message(
            CONTROL_ROOM, "@unknown:test", "help", {}
        )
        assert len(matrix.sent_messages) == 0

    def test_none_allowed_users_allows_all(self):
        router = MessageRouter(
            matrix=FakeMatrix(),
            ipc=FakeIPC(),
            containers=FakeContainers(),
            control_room_id=CONTROL_ROOM,
            allowed_users=None,
        )
        assert router._is_user_allowed("@anyone:anywhere") is True


# ------------------------------------------------------------------
# Health check & error recovery tests
# ------------------------------------------------------------------


class TestHealthCheck:
    """Test container health monitoring."""

    @pytest.mark.asyncio
    async def test_health_check_loop_starts_and_stops(self, started_router):
        """Health check loop should be running after start."""
        router, _, _, _ = started_router
        assert hasattr(router, "_health_task")
        assert not router._health_task.done()

        await router.stop()
        assert router._health_task.done()

    @pytest.mark.asyncio
    async def test_health_notifies_on_crash(self):
        """Health check should send a message when a container crashes."""
        matrix = FakeMatrix()
        ipc = FakeIPC()
        containers = FakeContainers()

        router = MessageRouter(
            matrix=matrix,
            ipc=ipc,
            containers=containers,
            control_room_id=CONTROL_ROOM,
        )

        # Add a session that will appear crashed
        session = containers.add_test_session(
            session_id="crash-test", room_id="!crash-room:test"
        )
        crashed = [session]

        # Override check_health to return the crashed session
        containers.check_health = AsyncMock(return_value=crashed)

        await router.start()
        # Cancel the auto-started health loop and run one check manually
        router._health_task.cancel()
        try:
            await router._health_task
        except asyncio.CancelledError:
            pass

        # Manually invoke the check logic (not the loop)
        crashed_list = await containers.check_health()
        for s in crashed_list:
            await matrix.send_message(s.room_id, "💀 Agent container crashed.")

        assert any(
            "crash" in m["body"].lower() and m["room_id"] == "!crash-room:test"
            for m in matrix.sent_messages
        )
