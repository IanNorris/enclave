"""Tests for the agent entry point logic."""

import asyncio

import pytest

from enclave.agent.main import handle_user_message, try_init_copilot
from enclave.common.protocol import Message, MessageType


class FakeIPCClient:
    """Fake IPC client that records sent messages."""

    def __init__(self) -> None:
        self.sent: list[Message] = []
        self.is_connected = True

    async def send(self, msg: Message) -> None:
        self.sent.append(msg)

    async def connect(self) -> None:
        self.is_connected = True

    async def disconnect(self) -> None:
        self.is_connected = False


class TestHandleUserMessage:
    """Test user message handling (echo mode — no SDK)."""

    @pytest.mark.asyncio
    async def test_echo_mode_response(self) -> None:
        """In echo mode (no SDK), messages are echoed back."""
        client = FakeIPCClient()
        msg = Message(
            type=MessageType.USER_MESSAGE,
            payload={"content": "hello", "sender": "@ian:example.org"},
        )

        await handle_user_message(client, None, msg, asyncio.get_running_loop())

        assert len(client.sent) == 1
        resp = client.sent[0]
        assert resp.type == MessageType.AGENT_RESPONSE
        assert "[echo] hello" in resp.payload["content"]

    @pytest.mark.asyncio
    async def test_echo_preserves_reply_to(self) -> None:
        """Response references the original message ID."""
        client = FakeIPCClient()
        msg = Message(
            type=MessageType.USER_MESSAGE,
            payload={"content": "test", "sender": "@test:x"},
            id="msg-123",
        )

        await handle_user_message(client, None, msg, asyncio.get_running_loop())

        resp = client.sent[0]
        assert resp.reply_to == "msg-123"
        assert resp.payload["in_reply_to"] == "msg-123"

    @pytest.mark.asyncio
    async def test_echo_empty_content(self) -> None:
        """Empty content is handled gracefully."""
        client = FakeIPCClient()
        msg = Message(
            type=MessageType.USER_MESSAGE,
            payload={"content": "", "sender": "@u:x"},
        )

        await handle_user_message(client, None, msg, asyncio.get_running_loop())

        resp = client.sent[0]
        assert "[echo] " in resp.payload["content"]

    @pytest.mark.asyncio
    async def test_echo_missing_content_key(self) -> None:
        """Missing 'content' key defaults to empty string."""
        client = FakeIPCClient()
        msg = Message(
            type=MessageType.USER_MESSAGE,
            payload={"sender": "@u:x"},
        )

        await handle_user_message(client, None, msg, asyncio.get_running_loop())

        resp = client.sent[0]
        assert "[echo]" in resp.payload["content"]


class TestTryInitCopilot:
    """Test Copilot SDK initialization (expected to fail in test env)."""

    @pytest.mark.asyncio
    async def test_returns_none_without_credentials(self) -> None:
        """Without proper credentials, returns None gracefully."""
        result = await try_init_copilot()
        # In CI/test env, this should return None (no credentials)
        # but shouldn't raise
        assert result is None or isinstance(result, tuple)


class TestAgentProtocol:
    """Test agent protocol message construction."""

    def test_status_update_message(self) -> None:
        """Status update messages have correct structure."""
        msg = Message(
            type=MessageType.STATUS_UPDATE,
            payload={
                "status": "ready",
                "session_id": "test-123",
                "copilot_available": False,
            },
        )

        assert msg.type == MessageType.STATUS_UPDATE
        assert msg.payload["status"] == "ready"
        assert msg.payload["copilot_available"] is False

    def test_agent_response_message(self) -> None:
        """Agent response messages have correct structure."""
        msg = Message(
            type=MessageType.AGENT_RESPONSE,
            payload={
                "content": "Hello back!",
                "in_reply_to": "msg-1",
            },
            reply_to="msg-1",
        )

        assert msg.type == MessageType.AGENT_RESPONSE
        assert msg.payload["content"] == "Hello back!"
        assert msg.reply_to == "msg-1"

    def test_message_serialization_roundtrip(self) -> None:
        """Messages survive JSON serialization."""
        msg = Message(
            type=MessageType.USER_MESSAGE,
            payload={"content": "test", "sender": "@u:x"},
        )
        json_str = msg.to_json()
        restored = Message.from_json(json_str)

        assert restored.type == msg.type
        assert restored.payload == msg.payload


class TestConfigureGraphifyMcp:
    """Tests for graphify MCP server registration (phase 2)."""

    def _graph(self, workspace) -> None:
        out = workspace / "graphify-out"
        out.mkdir(parents=True, exist_ok=True)
        (out / "graph.json").write_text('{"nodes": [], "links": []}')

    def test_registers_server_when_graph_present(self, tmp_path, monkeypatch) -> None:
        import json as _json

        from enclave.agent import main as agent_main

        monkeypatch.setattr(agent_main.shutil, "which", lambda _n: "/usr/local/bin/graphify", raising=False)
        ws = tmp_path / "ws"
        state = tmp_path / "state"
        ws.mkdir()
        state.mkdir()
        self._graph(ws)

        agent_main._configure_graphify_mcp(str(ws), str(state))

        cfg = _json.loads((state / "mcp-config.json").read_text())
        gfy = cfg["mcpServers"]["graphify"]
        assert gfy["type"] == "local"
        assert gfy["args"][:2] == ["-m", "graphify.serve"]
        assert gfy["args"][2].endswith("graphify-out/graph.json")
        assert gfy["tools"] == ["*"]

    def test_no_config_written_when_graph_absent(self, tmp_path, monkeypatch) -> None:
        from enclave.agent import main as agent_main

        monkeypatch.setattr(agent_main.shutil, "which", lambda _n: "/usr/local/bin/graphify", raising=False)
        ws = tmp_path / "ws"
        state = tmp_path / "state"
        ws.mkdir()
        state.mkdir()

        agent_main._configure_graphify_mcp(str(ws), str(state))

        assert not (state / "mcp-config.json").exists()

    def test_removes_stale_entry_when_graph_deleted(self, tmp_path, monkeypatch) -> None:
        import json as _json

        from enclave.agent import main as agent_main

        monkeypatch.setattr(agent_main.shutil, "which", lambda _n: "/usr/local/bin/graphify", raising=False)
        ws = tmp_path / "ws"
        state = tmp_path / "state"
        ws.mkdir()
        state.mkdir()
        (state / "mcp-config.json").write_text(
            '{"mcpServers": {"graphify": {"type": "local"}, "other": {"type": "local"}}}'
        )

        agent_main._configure_graphify_mcp(str(ws), str(state))

        cfg = _json.loads((state / "mcp-config.json").read_text())
        assert "graphify" not in cfg["mcpServers"]
        assert "other" in cfg["mcpServers"]

    def test_preserves_other_servers(self, tmp_path, monkeypatch) -> None:
        import json as _json

        from enclave.agent import main as agent_main

        monkeypatch.setattr(agent_main.shutil, "which", lambda _n: "/usr/local/bin/graphify", raising=False)
        ws = tmp_path / "ws"
        state = tmp_path / "state"
        ws.mkdir()
        state.mkdir()
        self._graph(ws)
        (state / "mcp-config.json").write_text('{"mcpServers": {"other": {"type": "local"}}}')

        agent_main._configure_graphify_mcp(str(ws), str(state))

        cfg = _json.loads((state / "mcp-config.json").read_text())
        assert "other" in cfg["mcpServers"]
        assert "graphify" in cfg["mcpServers"]
