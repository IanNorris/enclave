"""Message router: wires Matrix ↔ IPC ↔ agent containers.

Routes user messages from Matrix rooms to the correct agent container,
and routes agent responses back to Matrix. Handles control room commands.
"""

from __future__ import annotations

import asyncio
import os
import time
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

# Minimum interval between Matrix message edits (seconds)
_EDIT_THROTTLE = 1.5

# Max length for accumulated activity messages before starting a new one
_MAX_ACTIVITY_LEN = 3500


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

        # Track pending reaction event IDs for cleanup (🤔 → ✅)
        self._pending_reactions: dict[str, str] = {}

        # Streaming state per session
        # Key: session_id, Value: dict with streaming context
        self._streaming: dict[str, dict[str, Any]] = {}

        # Activity status message per session (editable tool/thinking display)
        self._activity_msg: dict[str, str] = {}  # session_id → Matrix event_id
        # Accumulated activity lines per session (appended, not replaced)
        self._activity_lines: dict[str, list[str]] = {}  # session_id → list of lines

        # Sub-agent thread tracking
        # session_id → event_id of the message that starts the sub-agent thread
        self._subagent_threads: dict[str, str] = {}

        # Pending messages queued during session restore (sent once agent is ready)
        self._pending_messages: dict[str, list[dict[str, Any]]] = {}

        # Sessions currently being restored (prevent double-restore)
        self._restoring: set[str] = set()

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

        # Start periodic health check
        self._health_task = asyncio.create_task(self._health_check_loop())

        log.info("Router started")

    async def stop(self) -> None:
        """Clean up."""
        if hasattr(self, "_health_task") and not self._health_task.done():
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass
        log.info("Router stopped")

    # ------------------------------------------------------------------
    # Periodic health monitoring
    # ------------------------------------------------------------------

    _HEALTH_INTERVAL = 60  # seconds

    async def _health_check_loop(self) -> None:
        """Periodically check container health and notify on crashes."""
        while True:
            try:
                await asyncio.sleep(self._HEALTH_INTERVAL)
                crashed = await self.containers.check_health()
                for session in crashed:
                    await self.matrix.send_message(
                        session.room_id,
                        "💀 Agent container crashed. Send a message to auto-restore.",
                    )
            except asyncio.CancelledError:
                return
            except Exception as e:
                log.error("Health check error: %s", e)

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
        attachments: list[dict[str, Any]] | None = None,
    ) -> None:
        """Handle an incoming Matrix message."""
        if not self._is_user_allowed(sender):
            log.debug("Ignoring message from unauthorized user: %s", sender)
            return

        event_id = self._get_event_id(source)
        thread_id = self._get_thread_id(source)

        if room_id == self.control_room_id:
            await self._handle_control_message(
                sender, body, source, event_id,
                attachments=attachments or [],
            )
        else:
            await self._handle_project_message(
                room_id, sender, body, source, thread_id, event_id,
                attachments=attachments or [],
            )

    async def _handle_control_message(
        self, sender: str, body: str, source: dict[str, Any],
        event_id: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> None:
        """Handle a message in the control room."""
        # Ignore media-only messages in control room (not commands)
        if attachments and body.startswith("[Sent a file:"):
            log.debug("Ignoring media message in control room: %s", body)
            return

        cmd = parse_command(body)
        if cmd is None:
            return

        log.info("Command from %s: %s %s", sender, cmd.command.value, cmd.raw_args)

        # 👀 ack → immediately replace with 🤔 + typing
        eyes_eid = None
        if event_id:
            eyes_eid = await self.matrix.send_reaction(
                self.control_room_id, event_id, "👀"
            )
        thinking_eid = None
        if event_id:
            thinking_eid = await self.matrix.send_reaction(
                self.control_room_id, event_id, "🤔"
            )
        if eyes_eid:
            await self.matrix.redact_event(self.control_room_id, eyes_eid)
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
            # ✅ Done — remove 🤔, add ✅, stop typing
            await self.matrix.set_typing(self.control_room_id, False)
            if thinking_eid:
                await self.matrix.redact_event(
                    self.control_room_id, thinking_eid
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
        attachments: list[dict[str, Any]] | None = None,
    ) -> None:
        """Handle a message in a project room — forward to the agent."""
        session = self.containers.get_session_by_room(room_id)

        if session is None:
            # Check for a stopped/persisted session to restore
            session = self.containers.get_any_session_by_room(room_id)
            if session is not None:
                # Queue the message so it's sent once the agent is ready
                self._pending_messages.setdefault(session.id, []).append({
                    "body": body,
                    "sender": sender,
                    "room_id": room_id,
                    "thread_id": thread_id,
                    "event_id": event_id,
                    "attachments": attachments,
                })
                # Only trigger restore if not already restoring
                if session.id not in self._restoring:
                    log.info("Restoring session %s for room %s", session.id, room_id)
                    await self._restore_session(session, room_id, event_id)
                else:
                    log.info("Session %s already restoring, queued message", session.id)
                return
            log.debug("No session for room %s", room_id)
            return

        if not self.ipc.is_connected(session.id):
            await self.matrix.send_message(
                room_id,
                "⏳ Agent is not connected yet. Please wait...",
                thread_event_id=thread_id,
            )
            return

        # 👀 ack → immediately replace with 🤔 + typing
        eyes_eid = None
        if event_id:
            eyes_eid = await self.matrix.send_reaction(
                room_id, event_id, "👀"
            )
        thinking_eid = None
        if event_id:
            thinking_eid = await self.matrix.send_reaction(
                room_id, event_id, "🤔"
            )
            self._pending_reactions[f"{room_id}:{event_id}"] = thinking_eid or ""
        if eyes_eid:
            await self.matrix.redact_event(room_id, eyes_eid)
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
                "attachments": attachments or [],
            },
        )

        sent = await self.ipc.send_to(session.id, msg)
        if not sent:
            await self.matrix.set_typing(room_id, False)
            if thinking_eid:
                await self.matrix.redact_event(room_id, thinking_eid)
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
        elif msg.type == MessageType.AGENT_DELTA:
            await self._handle_agent_delta(session, msg)
        elif msg.type == MessageType.AGENT_THINKING:
            await self._handle_agent_thinking(session, msg)
        elif msg.type == MessageType.TOOL_START:
            await self._handle_tool_start(session, msg)
        elif msg.type == MessageType.TOOL_COMPLETE:
            await self._handle_tool_complete(session, msg)
        elif msg.type == MessageType.SUBAGENT_STARTED:
            await self._handle_subagent_started(session, msg)
        elif msg.type == MessageType.SUBAGENT_COMPLETED:
            await self._handle_subagent_completed(session, msg)
        elif msg.type == MessageType.TURN_START:
            pass  # Tracked implicitly via other events
        elif msg.type == MessageType.TURN_END:
            pass  # Tracked implicitly via other events
        elif msg.type == MessageType.STATUS_UPDATE:
            await self._handle_agent_status(session, msg)
        elif msg.type == MessageType.PERMISSION_REQUEST:
            await self._handle_permission_request(session, msg)
        elif msg.type == MessageType.FILE_SEND:
            return await self._handle_file_send(session, msg)
        elif msg.type == MessageType.DOWNLOAD_REQUEST:
            return await self._handle_download_request(session, msg)
        else:
            log.debug(
                "Unhandled IPC message type from %s: %s",
                session_id,
                msg.type.value,
            )

        return None

    async def _handle_agent_delta(
        self, session: Session, msg: Message
    ) -> None:
        """Handle a streaming text delta — create or edit a Matrix message."""
        content = msg.payload.get("content", "")
        if not content:
            return

        thread_id = self._thread_events.get(session.id)
        stream = self._streaming.get(session.id)
        now = time.monotonic()

        if stream is None:
            # First delta — detach activity message (keep in chat) and start streaming
            self._activity_msg.pop(session.id, None)
            self._activity_lines.pop(session.id, None)
            # Send a new message as placeholder
            event_id = await self.matrix.send_message(
                session.room_id,
                content + " ▍",
                thread_event_id=thread_id,
            )
            self._streaming[session.id] = {
                "event_id": event_id,
                "last_edit": now,
                "content": content,
            }
        else:
            stream["content"] = content
            # Throttle edits
            if now - stream["last_edit"] >= _EDIT_THROTTLE and stream.get("event_id"):
                await self.matrix.edit_message(
                    session.room_id,
                    stream["event_id"],
                    content + " ▍",
                )
                stream["last_edit"] = now

    async def _handle_agent_thinking(
        self, session: Session, msg: Message
    ) -> None:
        """Handle an intent/thinking update — show as status message."""
        intent = msg.payload.get("intent", "")
        if not intent:
            return
        log.debug("Agent %s intent: %s", session.id, intent)
        thread_id = self._subagent_threads.get(session.id) or self._thread_events.get(session.id)
        status_text = f"-# 💭 {intent}"
        await self._update_activity(session, status_text, thread_id)

    async def _handle_tool_start(
        self, session: Session, msg: Message
    ) -> None:
        """Handle tool execution start — show tool name and description."""
        tool_name = msg.payload.get("tool_name", "unknown")
        description = msg.payload.get("description", "")
        # Skip internal/noisy tools
        if tool_name in ("report_intent",):
            return
        log.debug("Agent %s tool start: %s", session.id, tool_name)
        thread_id = self._subagent_threads.get(session.id) or self._thread_events.get(session.id)
        if description:
            status_text = f"-# 🔧 **{tool_name}**: {description}"
        else:
            status_text = f"-# 🔧 **{tool_name}**"
        await self._update_activity(session, status_text, thread_id)

    async def _handle_tool_complete(
        self, session: Session, msg: Message
    ) -> None:
        """Handle tool execution complete."""
        tool_name = msg.payload.get("tool_name", "unknown")
        success = msg.payload.get("success", True)
        if tool_name in ("report_intent",):
            return
        log.debug("Agent %s tool complete: %s (success=%s)", session.id, tool_name, success)
        # Keep typing indicator while more work may come
        await self.matrix.set_typing(session.room_id, True)

    async def _handle_subagent_started(
        self, session: Session, msg: Message
    ) -> None:
        """Handle sub-agent start — create a thread for its activity."""
        agent_name = msg.payload.get("agent_name", "sub-agent")
        description = msg.payload.get("description", "")
        log.info("Sub-agent started for %s: %s (%s)", session.id, agent_name, description)

        # Send a message that becomes the thread root for sub-agent activity
        thread_id = self._thread_events.get(session.id)
        label = f"🤖 **{agent_name}**"
        if description:
            label += f": {description}"
        event_id = await self.matrix.send_message(
            session.room_id, label, thread_event_id=thread_id,
        )
        if event_id:
            self._subagent_threads[session.id] = event_id

    async def _handle_subagent_completed(
        self, session: Session, msg: Message
    ) -> None:
        """Handle sub-agent completion — close its thread context."""
        agent_name = msg.payload.get("agent_name", "sub-agent")
        success = msg.payload.get("success", True)
        log.info("Sub-agent completed for %s: %s (success=%s)", session.id, agent_name, success)
        # Clear the sub-agent thread so further events go to the main thread
        self._subagent_threads.pop(session.id, None)
        # Clear any lingering activity message reference
        self._activity_msg.pop(session.id, None)
        self._activity_lines.pop(session.id, None)

    async def _update_activity(
        self, session: Session, text: str, thread_id: str | None
    ) -> None:
        """Append a line to the activity status message.

        Lines accumulate in a single message (edited in-place) until
        the combined text exceeds _MAX_ACTIVITY_LEN, at which point the
        current message is finalised and a new one is started.  Activity
        messages are **not** deleted — they stay in the chat history.
        """
        lines = self._activity_lines.setdefault(session.id, [])
        lines.append(text)
        combined = "\n".join(lines)

        existing = self._activity_msg.get(session.id)

        if existing and len(combined) <= _MAX_ACTIVITY_LEN:
            # Still fits — edit the existing message
            await self.matrix.edit_message(session.room_id, existing, combined)
        elif existing:
            # Over the limit — finalise current message and start a new one
            self._activity_msg.pop(session.id, None)
            self._activity_lines[session.id] = [text]
            event_id = await self.matrix.send_message(
                session.room_id, text, thread_event_id=thread_id,
            )
            if event_id:
                self._activity_msg[session.id] = event_id
        else:
            # No existing message — create one
            event_id = await self.matrix.send_message(
                session.room_id, combined, thread_event_id=thread_id,
            )
            if event_id:
                self._activity_msg[session.id] = event_id
        await self.matrix.set_typing(session.room_id, True)

    async def _handle_agent_response(
        self, session: Session, msg: Message
    ) -> None:
        """Forward the final agent response to the Matrix room."""
        content = msg.payload.get("content", "")
        if not content:
            return

        thread_id = self._thread_events.get(session.id)
        log.info(
            "Forwarding agent response to %s (thread=%s): %s",
            session.room_id, thread_id, content[:80],
        )

        # Stop typing and detach activity status (keep it in chat history)
        await self.matrix.set_typing(session.room_id, False)
        self._activity_msg.pop(session.id, None)
        self._activity_lines.pop(session.id, None)

        # If we have a streaming message, edit it with final content.
        # Otherwise, send a new message.
        stream = self._streaming.pop(session.id, None)
        if stream and stream.get("event_id"):
            await self.matrix.edit_message(
                session.room_id,
                stream["event_id"],
                content,
            )
        else:
            await self.matrix.send_message(
                session.room_id,
                content,
                thread_event_id=thread_id,
            )

        # Complete the emoji flow: remove 🤔, add ✅
        user_event_id = self._thread_events.pop(
            f"{session.id}:event_id", None
        )
        user_room_id = self._thread_events.pop(
            f"{session.id}:room_id", None
        )
        if user_event_id and user_room_id:
            thinking_key = f"{user_room_id}:{user_event_id}"
            thinking_eid = self._pending_reactions.pop(thinking_key, None)
            if thinking_eid:
                await self.matrix.redact_event(user_room_id, thinking_eid)
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

            # Flush any messages queued during session restore
            pending = self._pending_messages.pop(session.id, [])
            for queued in pending:
                log.info(
                    "Sending queued message to %s: %s",
                    session.id, queued["body"][:60],
                )
                # Send directly via IPC — do NOT re-enter
                # _handle_project_message which could trigger another restore
                flush_msg = Message(
                    type=MessageType.USER_MESSAGE,
                    payload={
                        "content": queued["body"],
                        "sender": queued["sender"],
                        "room_id": queued["room_id"],
                        "thread_id": queued.get("thread_id"),
                        "attachments": queued.get("attachments") or [],
                    },
                )
                sent = await self.ipc.send_to(session.id, flush_msg)
                if not sent:
                    log.warning(
                        "Failed to send queued message to %s", session.id
                    )

    async def _handle_file_send(
        self, session: Session, msg: Message
    ) -> Message | None:
        """Handle a file upload request from an agent."""
        file_path = msg.payload.get("file_path", "")
        body = msg.payload.get("body")

        if not file_path:
            return Message(
                type=MessageType.AGENT_RESPONSE,
                payload={"error": "No file_path provided"},
                reply_to=msg.id,
            )

        # Translate container path → host path
        # Container mounts workspace_path:/workspace, so
        # /workspace/foo.png → {workspace_path}/foo.png
        if file_path.startswith("/workspace/"):
            host_path = os.path.join(
                session.workspace_path, file_path[len("/workspace/"):]
            )
        elif file_path.startswith("/workspace"):
            host_path = session.workspace_path
        else:
            host_path = file_path

        log.debug("File send: container=%s host=%s", file_path, host_path)

        event_id = await self.matrix.upload_file(
            session.room_id, host_path, body=body
        )

        return Message(
            type=MessageType.AGENT_RESPONSE,
            payload={
                "sent": event_id is not None,
                "event_id": event_id,
            },
            reply_to=msg.id,
        )

    async def _handle_download_request(
        self, session: Session, msg: Message
    ) -> Message | None:
        """Handle a file download request from an agent."""
        url = msg.payload.get("url", "")
        dest = msg.payload.get("dest", "")

        if not url or not dest:
            return Message(
                type=MessageType.AGENT_RESPONSE,
                payload={"error": "url and dest are required"},
                reply_to=msg.id,
            )

        if url.startswith("mxc://"):
            success = await self.matrix.download_media(url, dest)
            return Message(
                type=MessageType.AGENT_RESPONSE,
                payload={"downloaded": success, "path": dest},
                reply_to=msg.id,
            )
        else:
            # External URL — agent should handle this itself
            return Message(
                type=MessageType.AGENT_RESPONSE,
                payload={"error": "Only mxc:// URLs supported for download"},
                reply_to=msg.id,
            )

    async def _handle_permission_request(
        self, session: Session, msg: Message
    ) -> None:
        """Handle a permission request from an agent (stub for Phase 2)."""
        log.info(
            "Permission request from %s: %s",
            session.id,
            msg.payload,
        )

    # ------------------------------------------------------------------
    # Session restoration
    # ------------------------------------------------------------------

    async def _restore_session(
        self, session: Session, room_id: str, event_id: str | None = None,
    ) -> None:
        """Restore a stopped session — recreate IPC socket and container."""
        self._restoring.add(session.id)
        try:
            await self.matrix.send_message(
                room_id, "🔄 Restoring session... please wait."
            )

            socket_path = await self.ipc.create_socket(session.id)
            session.socket_path = str(socket_path)

            started = await self.containers.start_session(session.id)
            if started:
                log.info("Session restored: %s", session.id)
            else:
                await self.matrix.send_message(
                    room_id, "❌ Failed to restore session."
                )
        finally:
            self._restoring.discard(session.id)

    # ------------------------------------------------------------------
    # Agent connect/disconnect
    # ------------------------------------------------------------------

    async def _on_agent_connect(self, session_id: str) -> None:
        """Called when an agent connects via IPC."""
        log.info("Agent connected: %s", session_id)

    async def _on_agent_disconnect(self, session_id: str) -> None:
        """Called when an agent disconnects."""
        # Clean up streaming state
        self._streaming.pop(session_id, None)

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

        socket_path = await self.ipc.create_socket(f"pending-{project_name}")

        session = await self.containers.create_session(
            name=project_name,
            room_id=room_id,
            socket_path=str(socket_path),
        )

        await self.ipc.remove_socket(f"pending-{project_name}")
        socket_path = await self.ipc.create_socket(session.id)
        session.socket_path = str(socket_path)

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

        await self.ipc.send_to(
            session_id,
            Message(type=MessageType.SHUTDOWN, payload={}),
        )

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
