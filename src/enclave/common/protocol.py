"""IPC protocol definitions shared between orchestrator and agent."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any
import json
import uuid


class MessageType(str, Enum):
    """Message types for orchestrator <-> agent communication."""

    # Orchestrator → Agent
    USER_MESSAGE = "user_message"
    PERMISSION_RESPONSE = "permission_response"
    PRIVILEGE_RESPONSE = "privilege_response"
    MOUNT_RESPONSE = "mount_response"
    SHUTDOWN = "shutdown"

    # Agent → Orchestrator
    AGENT_RESPONSE = "agent_response"
    AGENT_DELTA = "agent_delta"
    AGENT_THINKING = "agent_thinking"
    TOOL_START = "tool_start"
    TOOL_COMPLETE = "tool_complete"
    SUBAGENT_STARTED = "subagent_started"
    SUBAGENT_COMPLETED = "subagent_completed"
    TURN_START = "turn_start"
    TURN_END = "turn_end"
    PERMISSION_REQUEST = "permission_request"
    PRIVILEGE_REQUEST = "privilege_request"
    MOUNT_REQUEST = "mount_request"
    SUB_AGENT_REQUEST = "sub_agent_request"
    GUI_LAUNCH_REQUEST = "gui_launch_request"
    SCREENSHOT_REQUEST = "screenshot_request"
    FILE_SEND = "file_send"
    DOWNLOAD_REQUEST = "download_request"
    STATUS_UPDATE = "status_update"
    SCHEDULE_SET = "schedule_set"
    SCHEDULE_CANCEL = "schedule_cancel"
    TIMER_SET = "timer_set"
    TIMER_CANCEL = "timer_cancel"

    # Orchestrator → Agent (scheduled callbacks)
    SCHEDULE_TRIGGER = "schedule_trigger"
    TIMER_TRIGGER = "timer_trigger"
    # Memory
    MEMORY_STORE = "memory_store"
    MEMORY_QUERY = "memory_query"
    MEMORY_LIST = "memory_list"
    MEMORY_DELETE = "memory_delete"
    DREAM_REQUEST = "dream_request"
    DREAM_COMPLETE = "dream_complete"

    # File watching
    FILE_CHANGE = "file_change"

    # Token/cost tracking
    USAGE_REPORT = "usage_report"


@dataclass
class Message:
    """A message passed between orchestrator and agent over the IPC socket."""

    type: MessageType
    payload: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    reply_to: str | None = None

    def to_json(self) -> str:
        return json.dumps({
            "id": self.id,
            "type": self.type.value,
            "payload": self.payload,
            "reply_to": self.reply_to,
        })

    @classmethod
    def from_json(cls, data: str) -> Message:
        obj = json.loads(data)
        return cls(
            id=obj["id"],
            type=MessageType(obj["type"]),
            payload=obj.get("payload", {}),
            reply_to=obj.get("reply_to"),
        )
