"""Message router: wires Matrix ↔ IPC ↔ agent containers.

Routes user messages from Matrix rooms to the correct agent container,
and routes agent responses back to Matrix. Handles control room commands.
"""

from __future__ import annotations

from typing import Any

from enclave.common.logging import get_logger
from enclave.common.protocol import Message, MessageType
from enclave.orchestrator.commands import (
    CommandType,
    ParsedCommand,
    format_help,
    parse_command,
)
from enclave.orchestrator.container import ContainerManager, Session
from enclave.orchestrator.ipc import IPCServer
from enclave.orchestrator.matrix_client import EnclaveMatrixClient

log = get_logger("router")


class MessageRouter:
    """Routes messages between Matrix, IPC, and commands.

    The router is the central coordinator:
    - Control room messages → parsed as commands
    - Project room messages → forwarded to the agent container via IPC
    - Agent IPC responses  → forwarded to the Matrix project room
    """

    def __init__(
        self,
        matrix: EnclaveMatrixClient,
        ipc: IPCServer,
        containers: ContainerManager,
        control_room_id: str,
        space_id: str | None = None,
        allowed_users: list[str] | None = None,
    ):
        self.matrix = matrix
        self.ipc = ipc
        self.containers = containers
        self.control_room_id = control_room_id
        self.space_id = space_id
        self.allowed_users = allowed_users

        # Track thread event IDs per session for threading replies
        self._thread_events: dict[str, str] = {}

        # Track pending reaction event IDs for cleanup (🌧 → ✅)
        # Key: f"{room_id}:{msg_event_id}", Value: reaction event ID
        self._pending_reactions: dict[str, str] = {}

    async def start(self) -> None:
        """Wire up all the event handlers."""
        self.matrix.on_message(self._on_matrix_message)
        self.ipc.set_handler(self._on_ipc_message)
        self.ipc.on_connect(self._on_agent_connect)
        self.ipc.on_disconnect(self._on_agent_disconnect)

        # Send startup announcement — also establishes Megolm session
        await self.matrix.send_message(
            self.control_room_id,
            "🏰 Enclave orchestrator online. Type `help` for commands.",
        )
        log.info("Router started")

    async def stop(self) -> None:
        """Clean up."""
        log.info("Router stopped")

    def _is_user_allowed(self, sender: str) -> bool:
        """Check if a sender is allowed to use Enclave."""
        if self.allowed_users is None:
            return True
        return sender in self.allowed_users

    def _get_thread_id(self, source: dict[str, Any]) -> str | None:
        """Extract thread event ID from a Matrix message source."""
        content = source.get("content", {})
        relates = content.get("m.relates_to", {})
        if relates.get("rel_type") == "m.thread":
            return relates.get("event_id")
        return None

    def _get_event_id(self, source: dict[str, Any]) -> str | None:
        """Extract event ID from a Matrix message source."""
        return source.get("event_id")

    # ------------------------------------------------------------------
    # Matrix → Router
    # ------------------------------------------------------------------

    async def _on_matrix_message(
        self,
        room_id: str,
        sender: str,
        body: str,
        source: dict[str, Any],
    ) -> None:
        """Handle an incoming Matrix message."""
        if not self._is_user_allowed(sender):
            log.debug("Ignoring message from unauthorized user: %s", sender)
            return

        event_id = self._get_event_id(source)
        thread_id = self._get_thread_id(source)

        # 👀 Acknowledge receipt
        if event_id:
            await self.matrix.send_reaction(room_id, event_id, "👀")

        if room_id == self.control_room_id:
            await self._handle_control_message(
                sender, body, source, event_id
            )
        else:
            await self._handle_project_message(
                room_id, sender, body, source, thread_id, event_id
            )

    async def _handle_control_message(
        self, sender: str, body: str, source: dict[str, Any],
        event_id: str | None = None,
    ) -> None:
        """Handle a message in the control room."""
        cmd = parse_command(body)
        if cmd is None:
            return

        log.info("Command from %s: %s %s", sender, cmd.command.value, cmd.raw_args)

        # 🌧 Mark as processing + typing indicator
        rain_eid = None
        if event_id:
            rain_eid = await self.matrix.send_reaction(
                self.control_room_id, event_id, "🌧️"
            )
        await self.matrix.set_typing(self.control_room_id, True)

        try:
            if cmd.command == CommandType.HELP:
                await self._cmd_help()
            elif cmd.command == CommandType.PROJECT:
                await self._cmd_project(sender, cmd)
            elif cmd.command == CommandType.SESSIONS:
                await self._cmd_sessions()
            elif cmd.command == CommandType.KILL:
                await self._cmd_kill(cmd)
            elif cmd.command == CommandType.STATUS:
                await self._cmd_status()
            elif cmd.command == CommandType.UNKNOWN:
                await self._reply_control(
                    f"Unknown command: `{cmd.args[0] if cmd.args else '?'}`. "
                    f"Try `help` for available commands."
                )
            else:
                await self._reply_control(
                    f"Command `{cmd.command.value}` not yet implemented."
                )
        finally:
            # ✅ Done — remove 🌧, add ✅, stop typing
            await self.matrix.set_typing(self.control_room_id, False)
            if rain_eid:
                await self.matrix.redact_event(
                    self.control_room_id, rain_eid
                )
            if event_id:
                await self.matrix.send_reaction(
                    self.control_room_id, event_id, "✅"
                )

    async def _handle_project_message(
        self,
        room_id: str,
        sender: str,
        body: str,
        source: dict[str, Any],
        thread_id: str | None,
        event_id: str | None = None,
    ) -> None:
        """Handle a message in a project room — forward to the agent."""
        session = self.containers.get_session_by_room(room_id)
        if session is None:
            log.debug("No active session for room %s", room_id)
            return

        if not self.ipc.is_connected(session.id):
            await self.matrix.send_message(
                room_id,
                "⏳ Agent is not connected yet. Please wait...",
                thread_event_id=thread_id,
            )
            return

        # 🌧 Mark as processing + typing
        rain_eid = None
        if event_id:
            rain_eid = await self.matrix.send_reaction(
                room_id, event_id, "🌧️"
            )
            # Store rain reaction for cleanup when agent responds
            self._pending_reactions[f"{room_id}:{event_id}"] = rain_eid or ""
        await self.matrix.set_typing(room_id, True)

        # Store context for routing the response back
        if thread_id:
            self._thread_events[session.id] = thread_id
        # Store the user's event_id so we can ✅ it when the agent responds
        if event_id:
            self._thread_events[f"{session.id}:event_id"] = event_id
            self._thread_events[f"{session.id}:room_id"] = room_id

        msg = Message(
            type=MessageType.USER_MESSAGE,
            payload={
                "content": body,
                "sender": sender,
                "room_id": room_id,
                "thread_id": thread_id,
            },
        )

        sent = await self.ipc.send_to(session.id, msg)
        if not sent:
            await self.matrix.set_typing(room_id, False)
            if rain_eid:
                await self.matrix.redact_event(room_id, rain_eid)
            await self.matrix.send_message(
                room_id,
                "❌ Failed to reach the agent. It may have disconnected.",
                thread_event_id=thread_id,
            )

    # ------------------------------------------------------------------
    # IPC → Router (agent responses)
    # ------------------------------------------------------------------

    async def _on_ipc_message(
        self, session_id: str, msg: Message
    ) -> Message | None:
        """Handle a message from an agent container."""
        session = self.containers.get_session(session_id)
        if session is None:
            log.warning("Message from unknown session: %s", session_id)
            return None

        if msg.type == MessageType.AGENT_RESPONSE:
            await self._handle_agent_response(session, msg)
        elif msg.type == MessageType.STATUS_UPDATE:
            await self._handle_agent_status(session, msg)
        elif msg.type == MessageType.PERMISSION_REQUEST:
            await self._handle_permission_request(session, msg)
        else:
            log.debug(
                "Unhandled IPC message type from %s: %s",
                session_id,
                msg.type.value,
            )

        return None

    async def _handle_agent_response(
        self, session: Session, msg: Message
    ) -> None:
        """Forward an agent response to the Matrix room."""
        content = msg.payload.get("content", "")
        if not content:
            return

        thread_id = self._thread_events.get(session.id)
        log.info(
            "Forwarding agent response to %s (thread=%s): %s",
            session.room_id, thread_id, content[:80],
        )

        # Stop typing
        await self.matrix.set_typing(session.room_id, False)

        # Send the response
        await self.matrix.send_message(
            session.room_id,
            content,
            thread_event_id=thread_id,
        )

        # Complete the emoji flow: remove 🌧, add ✅
        user_event_id = self._thread_events.pop(
            f"{session.id}:event_id", None
        )
        user_room_id = self._thread_events.pop(
            f"{session.id}:room_id", None
        )
        if user_event_id and user_room_id:
            rain_key = f"{user_room_id}:{user_event_id}"
            rain_eid = self._pending_reactions.pop(rain_key, None)
            if rain_eid:
                await self.matrix.redact_event(user_room_id, rain_eid)
            await self.matrix.send_reaction(
                user_room_id, user_event_id, "✅"
            )

    async def _handle_agent_status(
        self, session: Session, msg: Message
    ) -> None:
        """Handle agent status updates."""
        status = msg.payload.get("status", "unknown")
        copilot = msg.payload.get("copilot_available", False)

        if status == "ready":
            mode = "🤖 Copilot" if copilot else "📝 Echo"
            await self.matrix.send_message(
                session.room_id,
                f"✅ Agent ready ({mode} mode). Start chatting!",
            )
            log.info(
                "Agent %s ready (copilot=%s)", session.id, copilot
            )

    async def _handle_permission_request(
        self, session: Session, msg: Message
    ) -> None:
        """Handle a permission request from an agent (stub for Phase 2)."""
        # TODO: Phase 2 — post approval request to Matrix
        log.info(
            "Permission request from %s: %s",
            session.id,
            msg.payload,
        )

    # ------------------------------------------------------------------
    # Agent connect/disconnect
    # ------------------------------------------------------------------

    async def _on_agent_connect(self, session_id: str) -> None:
        """Called when an agent connects via IPC."""
        log.info("Agent connected: %s", session_id)

    async def _on_agent_disconnect(self, session_id: str) -> None:
        """Called when an agent disconnects."""
        session = self.containers.get_session(session_id)
        if session and session.status == "running":
            await self.matrix.send_message(
                session.room_id,
                "⚠️ Agent disconnected.",
            )
        log.info("Agent disconnected: %s", session_id)

    # ------------------------------------------------------------------
    # Control room commands
    # ------------------------------------------------------------------

    async def _reply_control(
        self, text: str, html: str | None = None
    ) -> None:
        """Send a reply to the control room."""
        await self.matrix.send_message(
            self.control_room_id, text, html_body=html
        )

    async def _cmd_help(self) -> None:
        """Handle the help command."""
        await self._reply_control(format_help())

    async def _cmd_project(self, sender: str, cmd: ParsedCommand) -> None:
        """Handle the project command — create a new project session."""
        if not cmd.has_args:
            await self._reply_control(
                "Usage: `project <name>` — creates a new project session."
            )
            return

        project_name = cmd.args[0]

        # Create a Matrix room for the project
        room_id = await self.matrix.create_room(
            name=f"🏰 {project_name}",
            topic=f"Enclave project: {project_name}",
            invite=[sender],
            encrypted=True,
            space_id=self.space_id,
        )

        if room_id is None:
            await self._reply_control(
                f"❌ Failed to create room for **{project_name}**."
            )
            return

        # Create IPC socket
        socket_path = await self.ipc.create_socket(f"pending-{project_name}")

        # Create container session
        session = await self.containers.create_session(
            name=project_name,
            room_id=room_id,
            socket_path=str(socket_path),
        )

        # Rename socket to match session ID
        await self.ipc.remove_socket(f"pending-{project_name}")
        socket_path = await self.ipc.create_socket(session.id)
        session.socket_path = str(socket_path)

        # Start the container
        started = await self.containers.start_session(session.id)

        if started:
            await self._reply_control(
                f"✅ Project **{project_name}** created!\n"
                f"Room created and agent starting...\n"
                f"Session ID: `{session.id}`"
            )
        else:
            await self._reply_control(
                f"⚠️ Room created for **{project_name}** but container "
                f"failed to start.\nSession ID: `{session.id}`"
            )

    async def _cmd_sessions(self) -> None:
        """Handle the sessions command — list active sessions."""
        sessions = self.containers.list_sessions()
        if not sessions:
            await self._reply_control("No active sessions.")
            return

        lines = ["**Active Sessions:**\n"]
        for s in sessions:
            connected = "🟢" if self.ipc.is_connected(s.id) else "🔴"
            lines.append(
                f"  {connected} **{s.name}** — `{s.id}` ({s.status})"
            )
        await self._reply_control("\n".join(lines))

    async def _cmd_kill(self, cmd: ParsedCommand) -> None:
        """Handle the kill command — stop a session."""
        if not cmd.has_args:
            await self._reply_control(
                "Usage: `kill <session-id>` — stops and removes a session."
            )
            return

        session_id = cmd.args[0]

        # Send shutdown to agent
        await self.ipc.send_to(
            session_id,
            Message(type=MessageType.SHUTDOWN, payload={}),
        )

        # Stop container
        removed = await self.containers.remove_session(session_id)
        await self.ipc.remove_socket(session_id)

        if removed:
            await self._reply_control(
                f"✅ Session `{session_id}` stopped and removed."
            )
        else:
            await self._reply_control(
                f"❌ Session `{session_id}` not found."
            )

    async def _cmd_status(self) -> None:
        """Handle the status command — show system status."""
        active = self.containers.active_sessions()
        total = len(self.containers.list_sessions())
        connected = len(self.ipc.connected_sessions())

        await self._reply_control(
            f"**🏰 Enclave Status**\n\n"
            f"  Sessions: {total} total, {len(active)} running\n"
            f"  IPC: {connected} agents connected\n"
            f"  Matrix: {'connected' if self.matrix.client.logged_in else 'disconnected'}"
        )
