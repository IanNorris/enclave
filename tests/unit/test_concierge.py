"""Tests for the concierge session foundations."""

import asyncio
import tempfile

import pytest

from enclave.common.config import ContainerConfig, EnclaveConfig, load_config
from enclave.common.protocol import Message, MessageType
from enclave.orchestrator.session_manager import (
    SessionManager,
    CONCIERGE_SESSION_ID,
    is_concierge,
)


def _make_manager() -> SessionManager:
    tmp = tempfile.mkdtemp()
    cfg = ContainerConfig(
        workspace_base=tmp + "/ws",
        session_base=tmp + "/sess",
        socket_dir=tmp + "/sock",
    )
    return SessionManager(config=cfg)


def test_concierge_constants():
    assert CONCIERGE_SESSION_ID == "__concierge__"
    assert is_concierge(CONCIERGE_SESSION_ID)
    assert not is_concierge("some-project-1234")


def test_create_session_with_explicit_id():
    sm = _make_manager()
    session = asyncio.run(
        sm.create_session(
            name="Concierge",
            room_id="!room:example.org",
            socket_path="/tmp/c.sock",
            session_id=CONCIERGE_SESSION_ID,
        )
    )
    assert session.id == CONCIERGE_SESSION_ID
    assert sm.get_session(CONCIERGE_SESSION_ID) is session


def test_create_session_rejects_unsafe_id():
    sm = _make_manager()
    with pytest.raises(ValueError):
        asyncio.run(
            sm.create_session(
                name="bad",
                room_id="!r:x",
                socket_path="/tmp/b.sock",
                session_id="../evil",
            )
        )


def test_create_session_auto_id_unchanged():
    sm = _make_manager()
    session = asyncio.run(
        sm.create_session(
            name="My Project",
            room_id="!r:x",
            socket_path="/tmp/p.sock",
        )
    )
    assert session.id.startswith("my-project-")
    assert session.id != CONCIERGE_SESSION_ID


def test_concierge_action_message_type():
    msg = Message(type=MessageType.CONCIERGE_ACTION, payload={"action": "list_sessions"})
    round_trip = Message.from_json(msg.to_json())
    assert round_trip.type == MessageType.CONCIERGE_ACTION
    assert round_trip.payload["action"] == "list_sessions"


def test_concierge_config_defaults():
    cfg = EnclaveConfig()
    assert cfg.concierge.enabled is True
    assert cfg.concierge.profile == ""


def test_concierge_config_from_yaml():
    yaml_text = (
        "matrix:\n"
        "  homeserver: https://example.org\n"
        "  user_id: '@bot:example.org'\n"
        "  password: x\n"
        "concierge:\n"
        "  enabled: false\n"
        "  profile: light\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(yaml_text)
        path = f.name
    cfg = load_config(path)
    assert cfg.concierge.enabled is False
    assert cfg.concierge.profile == "light"
