"""Sub-agent manager: spawn child agents with Matrix threads.

Handles spawning sub-agents (e.g., search, code review) as separate
containers with their own Matrix threads. Routes results back to the
parent agent when complete.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

from enclave.common.logging import get_logger
from enclave.common.protocol import Message, MessageType

log = get_logger("sub_agents")


@dataclass
class SubAgent:
    """A sub-agent spawned by a parent agent."""

    id: str
    parent_session_id: str
    name: str
    purpose: str
    room_id: str           # Same room as parent
    thread_event_id: str   # Root event of the Matrix thread
    session_id: str = ""   # Container session ID (set after spawn)
    status: str = "pending"  # pending, running, completed, failed
    result: str = ""


class SubAgentManager:
    """Manages sub-agent lifecycle.

    When a parent agent requests a sub-agent:
    1. Creates a Matrix thread in the parent's room
    2. Spawns a new container for the sub-agent
    3. Routes sub-agent messages through the thread
    4. On completion, sends summary back to parent
    """

    def __init__(
        self,
        send_message: Callable[..., Awaitable[str | None]],
        create_container: Callable[..., Awaitable[str | None]],
        create_ipc_socket: Callable[[str], Awaitable[str]],
        send_to_agent: Callable[[str, Message], Awaitable[bool]],
    ):
        self.send_message = send_message
        self.create_container = create_container
        self.create_ipc_socket = create_ipc_socket
        self.send_to_agent = send_to_agent

        self._sub_agents: dict[str, SubAgent] = {}
        # Map: sub-agent session_id → parent session_id
        self._session_parent: dict[str, str] = {}

    async def spawn(
        self,
        parent_session_id: str,
        room_id: str,
        name: str,
        purpose: str,
        system_prompt: str = "",
        has_network: bool = False,
        has_workspace: bool = False,
    ) -> SubAgent | None:
        """Spawn a new sub-agent.

        Args:
            parent_session_id: The parent agent's session ID.
            room_id: Matrix room to create the thread in.
            name: Human-readable name for the sub-agent.
            purpose: What the sub-agent should do (initial prompt).
            system_prompt: Custom system prompt for the sub-agent.
            has_network: Whether the sub-agent gets network access.
            has_workspace: Whether the sub-agent gets workspace access.

        Returns:
            SubAgent on success, None on failure.
        """
        sub_id = f"sub-{uuid.uuid4().hex[:8]}"

        # Create thread in Matrix
        thread_root = await self.send_message(
            room_id,
            f"🤖 **Sub-agent: {name}**\n_{purpose}_",
        )
        if thread_root is None:
            log.error("Failed to create thread for sub-agent %s", name)
            return None

        sub = SubAgent(
            id=sub_id,
            parent_session_id=parent_session_id,
            name=name,
            purpose=purpose,
            room_id=room_id,
            thread_event_id=thread_root,
        )
        self._sub_agents[sub_id] = sub

        # Create IPC socket for sub-agent
        socket_path = await self.create_ipc_socket(sub_id)

        # Spawn container
        session_id = await self.create_container(
            name=f"sub-{name}",
            room_id=room_id,
            socket_path=str(socket_path),
            has_network=has_network,
            has_workspace=has_workspace,
        )

        if session_id is None:
            sub.status = "failed"
            await self.send_message(
                room_id,
                f"❌ Failed to spawn sub-agent: {name}",
                thread_event_id=thread_root,
            )
            return sub

        sub.session_id = session_id
        sub.status = "running"
        self._session_parent[session_id] = parent_session_id

        log.info(
            "Sub-agent spawned: %s (parent: %s, session: %s)",
            name, parent_session_id, session_id,
        )

        # Send the initial purpose as a user message to the sub-agent
        await self.send_to_agent(
            session_id,
            Message(
                type=MessageType.USER_MESSAGE,
                payload={
                    "content": purpose,
                    "sender": "orchestrator",
                    "room_id": room_id,
                    "thread_id": thread_root,
                    "is_sub_agent_init": True,
                },
            ),
        )

        return sub

    async def complete(
        self, sub_id: str, result: str
    ) -> None:
        """Mark a sub-agent as completed and send result to parent.

        Args:
            sub_id: The sub-agent ID.
            result: The sub-agent's result/summary text.
        """
        sub = self._sub_agents.get(sub_id)
        if sub is None:
            return

        sub.status = "completed"
        sub.result = result

        # Post completion to thread
        await self.send_message(
            sub.room_id,
            f"✅ **Sub-agent complete: {sub.name}**\n\n{result}",
            thread_event_id=sub.thread_event_id,
        )

        # Send result back to parent agent
        await self.send_to_agent(
            sub.parent_session_id,
            Message(
                type=MessageType.AGENT_RESPONSE,
                payload={
                    "content": f"[Sub-agent {sub.name}] {result}",
                    "from_sub_agent": sub.id,
                },
            ),
        )

        # Cleanup
        self._session_parent.pop(sub.session_id, None)
        log.info("Sub-agent completed: %s", sub.name)

    async def fail(self, sub_id: str, error: str) -> None:
        """Mark a sub-agent as failed."""
        sub = self._sub_agents.get(sub_id)
        if sub is None:
            return

        sub.status = "failed"

        await self.send_message(
            sub.room_id,
            f"❌ **Sub-agent failed: {sub.name}**\n_{error}_",
            thread_event_id=sub.thread_event_id,
        )

        self._session_parent.pop(sub.session_id, None)
        log.info("Sub-agent failed: %s — %s", sub.name, error)

    def get_sub_agent(self, sub_id: str) -> SubAgent | None:
        return self._sub_agents.get(sub_id)

    def get_by_session(self, session_id: str) -> SubAgent | None:
        """Get a sub-agent by its container session ID."""
        for sub in self._sub_agents.values():
            if sub.session_id == session_id:
                return sub
        return None

    def get_parent_session(self, session_id: str) -> str | None:
        """Get the parent session ID for a sub-agent's session."""
        return self._session_parent.get(session_id)

    def list_sub_agents(
        self, parent_session_id: str | None = None
    ) -> list[SubAgent]:
        """List sub-agents, optionally filtered by parent."""
        if parent_session_id:
            return [
                s for s in self._sub_agents.values()
                if s.parent_session_id == parent_session_id
            ]
        return list(self._sub_agents.values())

    def active_count(self, parent_session_id: str) -> int:
        """Count running sub-agents for a parent."""
        return sum(
            1 for s in self._sub_agents.values()
            if s.parent_session_id == parent_session_id and s.status == "running"
        )
