"""Tests for IPC server and client."""

import asyncio
from pathlib import Path

import pytest

from enclave.common.protocol import Message, MessageType
from enclave.orchestrator.ipc import IPCServer
from enclave.agent.ipc_client import IPCClient


@pytest.fixture
async def ipc_server(tmp_path: Path) -> IPCServer:
    """Create an IPC server with a temp socket directory."""
    server = IPCServer(tmp_path / "sockets")
    yield server
    await server.close_all()


class TestIPCServer:
    """Test IPC server socket management."""

    async def test_create_socket(self, ipc_server: IPCServer) -> None:
        path = await ipc_server.create_socket("test-session")
        assert path.exists()
        assert path.name == "test-session.sock"

    async def test_remove_socket(self, ipc_server: IPCServer) -> None:
        path = await ipc_server.create_socket("test-session")
        assert path.exists()
        await ipc_server.remove_socket("test-session")
        assert not path.exists()

    async def test_socket_path(self, ipc_server: IPCServer) -> None:
        path = ipc_server.socket_path("my-session")
        assert path.name == "my-session.sock"

    async def test_no_connection_initially(self, ipc_server: IPCServer) -> None:
        assert not ipc_server.is_connected("test-session")
        assert ipc_server.connected_sessions() == []

    async def test_send_to_disconnected_returns_false(self, ipc_server: IPCServer) -> None:
        msg = Message(type=MessageType.USER_MESSAGE, payload={"content": "hello"})
        result = await ipc_server.send_to("nonexistent", msg)
        assert result is False


class TestIPCRoundTrip:
    """Test full IPC communication between server and client."""

    async def test_connect_and_send(self, ipc_server: IPCServer) -> None:
        """Agent connects and sends a message to the server."""
        received: list[Message] = []

        async def handler(session_id: str, msg: Message) -> Message | None:
            received.append(msg)
            return Message(
                type=MessageType.AGENT_RESPONSE,
                payload={"echo": msg.payload.get("content")},
                reply_to=msg.id,
            )

        ipc_server.set_handler(handler)
        sock_path = await ipc_server.create_socket("test-session")

        client = IPCClient(str(sock_path))
        await client.connect()
        assert client.is_connected

        try:
            # Send a message and get a response
            response = await client.request(
                Message(
                    type=MessageType.AGENT_RESPONSE,
                    payload={"content": "hello from agent"},
                ),
                timeout=5.0,
            )

            assert len(received) == 1
            assert received[0].payload["content"] == "hello from agent"
            assert response.payload["echo"] == "hello from agent"
        finally:
            await client.disconnect()

    async def test_server_push_to_agent(self, ipc_server: IPCServer) -> None:
        """Server pushes a message to a connected agent."""
        received_by_agent: list[Message] = []

        sock_path = await ipc_server.create_socket("push-test")

        client = IPCClient(str(sock_path))

        async def on_user_message(msg: Message) -> Message | None:
            received_by_agent.append(msg)
            return None

        client.on_message(MessageType.USER_MESSAGE, on_user_message)
        await client.connect()

        try:
            # Wait for connection to be registered
            await asyncio.sleep(0.1)

            assert ipc_server.is_connected("push-test")

            # Server pushes to agent
            sent = await ipc_server.send_to(
                "push-test",
                Message(
                    type=MessageType.USER_MESSAGE,
                    payload={"content": "hello from orchestrator"},
                ),
            )
            assert sent is True

            # Give time for message to arrive
            await asyncio.sleep(0.2)
            assert len(received_by_agent) == 1
            assert received_by_agent[0].payload["content"] == "hello from orchestrator"
        finally:
            await client.disconnect()

    async def test_connect_disconnect_callbacks(self, ipc_server: IPCServer) -> None:
        """Test connect and disconnect callbacks."""
        connected: list[str] = []
        disconnected: list[str] = []

        async def on_connect(sid: str) -> None:
            connected.append(sid)

        async def on_disconnect(sid: str) -> None:
            disconnected.append(sid)

        ipc_server.on_connect(on_connect)
        ipc_server.on_disconnect(on_disconnect)

        sock_path = await ipc_server.create_socket("cb-test")
        client = IPCClient(str(sock_path))
        await client.connect()

        await asyncio.sleep(0.1)
        assert "cb-test" in connected

        await client.disconnect()
        await asyncio.sleep(0.2)
        assert "cb-test" in disconnected

    async def test_multiple_sessions(self, ipc_server: IPCServer) -> None:
        """Multiple agents connect simultaneously."""
        messages: dict[str, list[Message]] = {"s1": [], "s2": []}

        async def handler(session_id: str, msg: Message) -> Message | None:
            messages[session_id].append(msg)
            return None

        ipc_server.set_handler(handler)

        path1 = await ipc_server.create_socket("s1")
        path2 = await ipc_server.create_socket("s2")

        client1 = IPCClient(str(path1))
        client2 = IPCClient(str(path2))
        await client1.connect()
        await client2.connect()

        try:
            await asyncio.sleep(0.1)
            assert len(ipc_server.connected_sessions()) == 2

            await client1.send(Message(type=MessageType.STATUS_UPDATE, payload={"from": "s1"}))
            await client2.send(Message(type=MessageType.STATUS_UPDATE, payload={"from": "s2"}))

            await asyncio.sleep(0.2)
            assert len(messages["s1"]) == 1
            assert len(messages["s2"]) == 1
            assert messages["s1"][0].payload["from"] == "s1"
            assert messages["s2"][0].payload["from"] == "s2"
        finally:
            await client1.disconnect()
            await client2.disconnect()

    async def test_connected_sessions_list(self, ipc_server: IPCServer) -> None:
        """connected_sessions reflects actual state."""
        path = await ipc_server.create_socket("list-test")
        client = IPCClient(str(path))
        await client.connect()
        await asyncio.sleep(0.1)

        assert "list-test" in ipc_server.connected_sessions()

        await client.disconnect()
        await asyncio.sleep(0.2)

        assert "list-test" not in ipc_server.connected_sessions()
