"""Tests for IPC protocol messages."""

import json

from enclave.common.protocol import Message, MessageType


class TestMessageType:
    """Test MessageType enum."""

    def test_user_message_value(self) -> None:
        assert MessageType.USER_MESSAGE.value == "user_message"

    def test_agent_response_value(self) -> None:
        assert MessageType.AGENT_RESPONSE.value == "agent_response"

    def test_from_string(self) -> None:
        assert MessageType("permission_request") == MessageType.PERMISSION_REQUEST

    def test_all_types_are_strings(self) -> None:
        for mt in MessageType:
            assert isinstance(mt.value, str)


class TestMessage:
    """Test Message serialization."""

    def test_create_message(self) -> None:
        msg = Message(type=MessageType.USER_MESSAGE, payload={"content": "hello"})
        assert msg.type == MessageType.USER_MESSAGE
        assert msg.payload == {"content": "hello"}
        assert msg.id  # auto-generated UUID
        assert msg.reply_to is None

    def test_to_json(self) -> None:
        msg = Message(
            type=MessageType.USER_MESSAGE,
            payload={"content": "test"},
            id="test-id-123",
        )
        data = json.loads(msg.to_json())
        assert data["id"] == "test-id-123"
        assert data["type"] == "user_message"
        assert data["payload"]["content"] == "test"
        assert data["reply_to"] is None

    def test_from_json(self) -> None:
        raw = json.dumps({
            "id": "abc-123",
            "type": "agent_response",
            "payload": {"text": "hi"},
            "reply_to": "orig-456",
        })
        msg = Message.from_json(raw)
        assert msg.id == "abc-123"
        assert msg.type == MessageType.AGENT_RESPONSE
        assert msg.payload == {"text": "hi"}
        assert msg.reply_to == "orig-456"

    def test_roundtrip(self) -> None:
        original = Message(
            type=MessageType.PERMISSION_REQUEST,
            payload={"path": "/home/ian/projects/foo", "access": "read-write"},
        )
        restored = Message.from_json(original.to_json())
        assert restored.id == original.id
        assert restored.type == original.type
        assert restored.payload == original.payload
        assert restored.reply_to == original.reply_to

    def test_unique_ids(self) -> None:
        msg1 = Message(type=MessageType.USER_MESSAGE)
        msg2 = Message(type=MessageType.USER_MESSAGE)
        assert msg1.id != msg2.id

    def test_reply_to(self) -> None:
        original = Message(type=MessageType.USER_MESSAGE, payload={"content": "hello"})
        reply = Message(
            type=MessageType.AGENT_RESPONSE,
            payload={"text": "hi back"},
            reply_to=original.id,
        )
        assert reply.reply_to == original.id
