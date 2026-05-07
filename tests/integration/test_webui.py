"""Integration tests for the web UI API routes.

Tests the ControlServer subscribe/notify and send flows.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from enclave.orchestrator.control import ControlServer


@pytest.fixture
def control_socket_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


def _make_mock_router(session_ids: list[str]) -> MagicMock:
    """Create a mock router with the expected interface."""
    router = MagicMock()
    mock_sessions = {}
    for sid in session_ids:
        s = MagicMock()
        s.session_id = sid
        s.status = "running"
        mock_sessions[sid] = s
    router.containers.get_session.side_effect = lambda sid: mock_sessions.get(sid)
    router.containers.list_sessions.return_value = list(mock_sessions.values())
    router.inject_message = AsyncMock(return_value=True)
    return router


class TestControlServerSubscribe:
    """Test the control socket subscribe/notify flow."""

    @pytest.mark.asyncio
    async def test_subscribe_receives_notifications(self, control_socket_dir):
        """Subscriber receives delta/thinking/tool events from ControlServer."""
        socket_path = control_socket_dir / "control.sock"
        router = _make_mock_router(["test-session"])

        server = ControlServer(str(socket_path), router)
        await server.start()

        try:
            received = []

            reader, writer = await asyncio.open_unix_connection(str(socket_path))

            # Send subscribe action
            subscribe_msg = json.dumps({
                "action": "subscribe",
                "session": "test-session",
            }) + "\n"
            writer.write(subscribe_msg.encode())
            await writer.drain()

            # Read the subscribed response
            line = await asyncio.wait_for(reader.readline(), timeout=5)
            resp = json.loads(line)
            assert resp.get("ok") is True
            assert resp.get("type") == "subscribed"

            # Emit notifications
            server.notify_delta("test-session", "Hello world")
            server.notify_thinking("test-session", "I'm thinking...", "delta")
            server.notify_tool_start("test-session", "bash", "ls -la")
            server.notify_tool_complete("test-session", "bash", True)

            # Read notifications (4 events)
            for _ in range(4):
                line = await asyncio.wait_for(reader.readline(), timeout=5)
                msg = json.loads(line)
                received.append(msg)

            types = [m.get("type") for m in received]
            assert "delta" in types
            assert "thinking" in types
            assert "tool_start" in types
            assert "tool_complete" in types

            delta_msg = next(m for m in received if m["type"] == "delta")
            assert delta_msg["content"] == "Hello world"

            tool_msg = next(m for m in received if m["type"] == "tool_complete")
            assert tool_msg["name"] == "bash"
            assert tool_msg["success"] is True

            writer.close()
            await writer.wait_closed()

        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_subscriber_cleanup_on_disconnect(self, control_socket_dir):
        """Disconnecting cleans up subscriber."""
        socket_path = control_socket_dir / "control.sock"
        router = _make_mock_router(["test-session"])

        server = ControlServer(str(socket_path), router)
        await server.start()

        try:
            reader, writer = await asyncio.open_unix_connection(str(socket_path))

            subscribe_msg = json.dumps({
                "action": "subscribe",
                "session": "test-session",
            }) + "\n"
            writer.write(subscribe_msg.encode())
            await writer.drain()

            # Read OK
            await asyncio.wait_for(reader.readline(), timeout=5)
            assert len(server._subscribers.get("test-session", set())) == 1

            writer.close()
            await writer.wait_closed()

            # Give cleanup time
            await asyncio.sleep(0.5)

            # Emit after disconnect — should not raise
            server.notify_delta("test-session", "after disconnect")

        finally:
            await server.stop()


class TestControlServerSend:
    """Test sending messages via control socket."""

    @pytest.mark.asyncio
    async def test_send_calls_inject_message(self, control_socket_dir):
        """Sending a message via control socket calls router.inject_message."""
        socket_path = control_socket_dir / "control.sock"
        router = _make_mock_router(["test-session"])

        server = ControlServer(str(socket_path), router)
        await server.start()

        try:
            reader, writer = await asyncio.open_unix_connection(str(socket_path))

            send_msg = json.dumps({
                "action": "send",
                "session": "test-session",
                "content": "Hello from test",
            }) + "\n"
            writer.write(send_msg.encode())
            await writer.drain()

            # Read ack
            line = await asyncio.wait_for(reader.readline(), timeout=5)
            resp = json.loads(line)
            assert resp.get("ok") is True
            assert resp.get("type") == "ack"

            # Verify inject_message was called
            await asyncio.sleep(0.2)
            router.inject_message.assert_called_once()
            call_args = router.inject_message.call_args
            assert call_args[0][0] == "test-session"

            writer.close()
            await writer.wait_closed()

        finally:
            await server.stop()

