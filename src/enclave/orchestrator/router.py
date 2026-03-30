"""Message router: wires Matrix ↔ IPC ↔ agent containers.

Routes user messages from Matrix rooms to the correct agent container,
and routes agent responses back to Matrix. Handles control room commands.
"""

from __future__ import annotations

import asyncio
import html as _html_mod
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from enclave.common.audit import AuditLog
from enclave.common.config import UserMapping
from enclave.common.cost_tracker import CostTracker
from enclave.common.logging import get_logger
from enclave.common.protocol import Message, MessageType
from enclave.orchestrator.approval import ApprovalManager
from enclave.orchestrator.commands import (
    CommandType,
    ParsedCommand,
    format_help,
    parse_command,
)
from enclave.orchestrator.container import ContainerManager, Session
from enclave.orchestrator.ipc import IPCServer
from enclave.orchestrator.matrix_client import EnclaveMatrixClient
from enclave.orchestrator.permissions import (
    PermissionDB,
    PermissionScope,
    PermissionType,
    RequestStatus,
)
from enclave.orchestrator.priv_client import PrivBrokerClient
from enclave.orchestrator.scheduler import Scheduler, ScheduleEntry, TimerEntry
from enclave.orchestrator.display import DisplayManager
from enclave.orchestrator.memory import MemoryStore
from enclave.orchestrator.watcher import WorkspaceWatcher
from enclave.orchestrator.control import ControlServer

log = get_logger("router")

# Minimum interval between Matrix message edits (seconds)
_EDIT_THROTTLE = 1.5

# Max length for accumulated activity messages before starting a new one
_MAX_ACTIVITY_LEN = 3500


def _html_escape(text: str) -> str:
    """Escape text for safe inclusion in HTML."""
    return _html_mod.escape(text, quote=False)


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
        user_mappings: list[UserMapping] | None = None,
        data_dir: str = "",
        priv_broker_socket: str = "/run/enclave-priv/broker.sock",
        approval_timeout: float = 300.0,
        idle_timeout: int = 7200,
        memory_config: Any | None = None,
    ):
        self.matrix = matrix
        self.ipc = ipc
        self.containers = containers
        self.control_room_id = control_room_id
        self.space_id = space_id
        self.allowed_users = allowed_users
        self._user_mappings = {u.matrix_id: u for u in (user_mappings or [])}

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

        # Sub-agent parent tracking: sub_session_id → parent_session_id
        self._subagent_parents: dict[str, str] = {}

        # Pending messages queued during session restore (sent once agent is ready)
        self._pending_messages: dict[str, list[dict[str, Any]]] = {}

        # Sessions currently being restored (prevent double-restore)
        self._restoring: set[str] = set()

        # File watchers for workspace change notifications
        self._watchers: dict[str, WorkspaceWatcher] = {}  # session_id → watcher

        # Projects currently being created (prevent double room creation)
        self._creating_projects: set[str] = set()

        # Generic poll awaits: poll_event_id → (asyncio.Event, result list)
        # Used for profile selection polls (and potentially other non-approval polls)
        self._generic_polls: dict[str, tuple[asyncio.Event, list[str]]] = {}

        # Rooms waiting for a user to join before sending queued messages
        # room_id → list of message strings to send once the user joins
        self._awaiting_join: dict[str, list[str]] = {}

        # Turn timing — detect stalled turns
        self._turn_start_time: dict[str, float] = {}

        # ── Privilege & permission system ──
        import os
        db_path = os.path.join(
            data_dir or os.path.expanduser("~/.local/share/enclave"),
            "permissions.db",
        )
        self._perm_db = PermissionDB(db_path)

        self._approval = ApprovalManager(
            permission_db=self._perm_db,
            send_message=self.matrix.send_message,
            send_reaction=self.matrix.send_reaction,
            send_poll=self.matrix.send_poll,
            end_poll=self.matrix.end_poll,
            timeout=approval_timeout,
        )

        self._priv_client = PrivBrokerClient(socket_path=priv_broker_socket)

        # Scheduler for cron jobs and timers
        scheduler_dir = os.path.join(
            data_dir or os.path.expanduser("~/.local/share/enclave"),
            "scheduler",
        )
        self._scheduler = Scheduler(
            data_dir=scheduler_dir,
            on_schedule_fire=self._on_schedule_fire,
            on_timer_fire=self._on_timer_fire,
        )

        # Track workspaces that have shared mount propagation set up
        self._propagation_ready: set[str] = set()

        # Display manager for desktop interaction
        self._display = DisplayManager()
        self._display.detect_session()

        # ── Idle timeout ──
        self._last_activity: dict[str, float] = {}  # session_id → monotonic timestamp
        self._idle_timeout = idle_timeout  # seconds, 0 = disabled

        # ── Memory stores (per user) ──
        self._memory_stores: dict[str, MemoryStore] = {}  # matrix_user_id → store
        self._memory_config = memory_config
        self._data_dir = data_dir or os.path.expanduser("~/.local/share/enclave")

        # ── Audit log ──
        self._audit = AuditLog(self._data_dir)

        # ── Cost / token tracking ──
        self._cost = CostTracker(self._data_dir)

        # ── Control socket for external message injection ──
        control_sock = os.path.join(self._data_dir, "control.sock")
        self._control = ControlServer(control_sock, self)

    async def start(self) -> None:
        """Wire up all the event handlers."""
        self.matrix.on_message(self._on_matrix_message)
        self.matrix.on_user_join(self._on_user_join)
        self.matrix.on_reaction(self._on_matrix_reaction)
        self.matrix.on_poll_response(self._on_poll_response)
        self.ipc.set_handler(self._on_ipc_message)
        self.ipc.on_connect(self._on_agent_connect)
        self.ipc.on_disconnect(self._on_agent_disconnect)

        # Send startup announcement — also establishes Megolm session
        # The bot may not have joined the control room yet (awaiting invite).
        if self.control_room_id not in self.matrix.client.rooms:
            log.warning(
                "Control room %s not joined — waiting for invite", self.control_room_id
            )
        else:
            await self.matrix.send_message(
                self.control_room_id,
                "🏰 Enclave orchestrator online. Type `help` for commands.",
            )

        # Connect to privilege broker (non-blocking — broker may not be running)
        if not await self._priv_client.connect():
            log.warning("Privilege broker not available — sudo requests will fail")
        else:
            log.info("Connected to privilege broker")

        # Start periodic health check
        self._health_task = asyncio.create_task(self._health_check_loop())

        # Start scheduler
        await self._scheduler.start()

        # Start control socket
        await self._control.start()

        # Auto-restore sessions that were running before last shutdown
        await self._auto_restore_sessions()

        log.info("Router started")

    async def stop(self) -> None:
        """Clean up — save session state for restore on next start."""
        # Save current session state so we know what was running
        self.containers._save_sessions()
        log.info("Session state saved for restore")

        if hasattr(self, "_health_task") and not self._health_task.done():
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass
        await self._scheduler.stop()
        await self._control.stop()
        await self._priv_client.disconnect()
        self._perm_db.close()
        log.info("Router stopped")

    async def inject_message(self, session_id: str, content: str) -> bool:
        """Inject a user message into a session (from control socket).

        Also echoes the message to the agent's Matrix room so users can
        follow the conversation.
        """
        session = self.containers.get_session(session_id)
        if not session:
            return False
        msg = Message(
            type=MessageType.USER_MESSAGE,
            payload={
                "content": content,
                "sender": "control",
                "room_id": session.room_id,
                "thread_id": None,
                "attachments": [],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )
        sent = await self.ipc.send_to(session_id, msg)
        if sent:
            self._touch_activity(session_id)
            log.info("Injected control message to %s: %s", session_id, content[:80])
            # Echo to Matrix so the user can see what was sent
            await self.matrix.send_message(
                session.room_id,
                content,
                html_body=f"<i>{_html_escape(content)}</i>",
            )
        return sent

    async def _auto_restore_sessions(self) -> None:
        """Restore sessions that were running before last shutdown/reboot.

        Called during startup. Walks through all sessions marked
        "was_running" and restarts their containers.
        """
        to_restore = self.containers.sessions_needing_restore()
        if not to_restore:
            log.info("No sessions to auto-restore")
            return

        log.info("Auto-restoring %d sessions from previous run", len(to_restore))
        await self._reply_control(
            f"🔄 Restoring {len(to_restore)} sessions from before shutdown..."
        )

        restored = 0
        failed = 0
        for session in to_restore:
            try:
                log.info("Auto-restoring session %s (%s)", session.id, session.name)

                # Mark as stopped first so _restore_session can work
                session.status = "stopped"

                # Create IPC socket
                socket_path = await self.ipc.create_socket(session.id)
                session.socket_path = str(socket_path)

                # Set up shared propagation
                await self._ensure_propagation(session)

                # Start the container
                started, error = await self.containers.start_session(session.id)
                if started:
                    restored += 1
                    log.info("Auto-restored session %s", session.id)
                    self._audit.log(
                        "session_restored", session_id=session.id,
                        name=session.name,
                    )
                    await self.matrix.send_message(
                        session.room_id,
                        "🔄 Session restored after system restart.",
                    )
                else:
                    failed += 1
                    session.status = "stopped"
                    log.warning(
                        "Failed to auto-restore session %s: %s",
                        session.id, error,
                    )
            except Exception as e:
                failed += 1
                session.status = "stopped"
                log.error("Exception restoring session %s: %s", session.id, e)

        summary = f"✅ Restored {restored}/{len(to_restore)} sessions"
        if failed:
            summary += f" ({failed} failed)"
        await self._reply_control(summary)
        log.info(summary)

    # ------------------------------------------------------------------
    # Periodic health monitoring
    # ------------------------------------------------------------------

    _HEALTH_INTERVAL = 60  # seconds
    _STALL_THRESHOLD = 300  # seconds — warn if a turn takes longer than this

    async def _health_check_loop(self) -> None:
        """Periodically check container health and notify on crashes/stalls."""
        while True:
            try:
                await asyncio.sleep(self._HEALTH_INTERVAL)

                # Notify systemd watchdog that the event loop is alive
                try:
                    from systemd.daemon import notify
                    notify("WATCHDOG=1")
                except Exception:
                    pass

                crashed = await self.containers.check_health()
                for session in crashed:
                    await self.matrix.send_message(
                        session.room_id,
                        "💀 Agent container crashed. Send a message to auto-restore.",
                    )

                # Check for stalled turns
                now = time.monotonic()
                for sid, start in list(self._turn_start_time.items()):
                    elapsed = now - start
                    if elapsed > self._STALL_THRESHOLD:
                        session = self.containers.get_session(sid)
                        if session:
                            log.warning(
                                "Agent %s turn stalled (%.0fs)", sid, elapsed
                            )
                            await self.matrix.send_message(
                                session.room_id,
                                f"⚠️ Agent appears stalled ({int(elapsed)}s with no response). "
                                "Try sending another message to nudge it.",
                            )
                        # Remove so we don't spam the warning
                        self._turn_start_time.pop(sid, None)

                # Check for idle sessions (no activity within timeout)
                if self._idle_timeout > 0:
                    await self._check_idle_sessions()

                # Periodically persist session state (crash recovery)
                self.containers._save_sessions()
            except asyncio.CancelledError:
                return
            except Exception as e:
                log.error("Health check error: %s", e)

    def _touch_activity(self, session_id: str) -> None:
        """Record that a session had activity (prevents idle shutdown)."""
        self._last_activity[session_id] = time.monotonic()

    async def _check_idle_sessions(self) -> None:
        """Stop sessions that have been idle beyond the timeout.

        A session is idle if:
        - No user/agent messages within idle_timeout seconds
        - Not currently in a turn (no active _turn_start_time)
        - Container has no busy subprocesses (via podman top)
        """
        if self._idle_timeout <= 0:
            return
        now = time.monotonic()
        for session in self.containers.active_sessions():
            sid = session.id

            # Skip sessions in active turns
            if sid in self._turn_start_time:
                continue

            last = self._last_activity.get(sid)
            if last is None:
                # First check — set the timestamp and skip
                self._last_activity[sid] = now
                continue

            elapsed = now - last
            if elapsed < self._idle_timeout:
                continue

            # Check if the container has busy processes
            if await self._container_has_processes(sid):
                log.debug("Session %s idle but has running processes", sid)
                continue

            log.info(
                "Session %s idle for %.0fs — shutting down", sid, elapsed
            )

            # Trigger auto-dreaming before shutdown if enabled
            if self._memory_config and self._memory_config.auto_dreaming:
                await self._trigger_dream_on_shutdown(session)

            await self.matrix.send_message(
                session.room_id,
                "💤 Session idle — shutting down. Send a message to restart.",
            )
            await self.containers.stop_session(sid)
            self._last_activity.pop(sid, None)

    async def _container_has_processes(self, session_id: str) -> bool:
        """Check if a container has non-trivial running processes."""
        try:
            result = await asyncio.create_subprocess_exec(
                self.containers.config.runtime, "top", session_id,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(result.communicate(), timeout=5)
            if result.returncode != 0:
                return False
            lines = stdout.decode().strip().split("\n")
            # podman top output: header + one line per process
            # If more than 2 processes (init + agent), something is running
            return len(lines) > 3
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Memory store helpers
    # ------------------------------------------------------------------

    def _get_memory_store(self, matrix_user_id: str) -> MemoryStore | None:
        """Get or create memory store for a user. Returns None if disabled."""
        if not self._memory_config or not self._memory_config.auto_memory:
            return None
        if matrix_user_id not in self._memory_stores:
            self._memory_stores[matrix_user_id] = MemoryStore(
                self._data_dir, matrix_user_id
            )
        return self._memory_stores[matrix_user_id]

    def _get_user_for_session(self, session: Any) -> str:
        """Find the Matrix user ID that owns a session (by room mapping)."""
        for uid, mapping in self._user_mappings.items():
            # Check if this user's sessions include this room
            if mapping.allowed_rooms == ["*"] or session.room_id in mapping.allowed_rooms:
                return uid
        # Fallback — return first user
        if self._user_mappings:
            return next(iter(self._user_mappings))
        return ""

    async def _trigger_dream_on_shutdown(self, session: Any) -> None:
        """Send a dream request to the agent before shutdown."""
        if not self.ipc.is_connected(session.id):
            return
        try:
            await self.ipc.send_to(session.id, Message(
                type=MessageType.DREAM_REQUEST,
                payload={"reason": "session_idle_shutdown"},
            ))
            # Give the agent a few seconds to extract and send memories
            await asyncio.sleep(5)
        except Exception as e:
            log.warning("Dream trigger failed for %s: %s", session.id, e)

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

        log.info("Command from %s (event %s): %s %s", sender, event_id, cmd.command.value, cmd.raw_args)
        self._audit.log(
            "command", user=sender,
            command=cmd.command.value, args=cmd.raw_args,
        )

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
                # Project creation needs poll responses from future syncs,
                # so run as a background task to avoid blocking the sync loop.
                asyncio.create_task(self._cmd_project_with_cleanup(
                    sender, cmd, event_id, thinking_eid,
                ))
                return  # cleanup handled by the task
            elif cmd.command == CommandType.SESSIONS:
                await self._cmd_sessions()
            elif cmd.command == CommandType.KILL:
                await self._cmd_kill(cmd)
            elif cmd.command == CommandType.STATUS:
                await self._cmd_status()
            elif cmd.command == CommandType.PERMS:
                await self._cmd_perms(cmd)
            elif cmd.command == CommandType.REVOKE:
                await self._cmd_revoke(cmd)
            elif cmd.command == CommandType.CLEANUP:
                await self._cmd_cleanup(cmd)
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
        # Check if this is a custom pattern reply for an approval
        awaiting_req = self._approval.get_awaiting_pattern(room_id)
        if awaiting_req is not None and body.strip():
            resolved = self._approval.handle_custom_pattern(
                awaiting_req, body.strip(), sender
            )
            if resolved:
                await self.matrix.send_message(
                    room_id,
                    f"✅ Custom pattern set: `{body.strip()}`",
                )
                return

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
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

        sent = await self.ipc.send_to(session.id, msg)
        self._touch_activity(session.id)
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

        # Any IPC message counts as session activity
        self._touch_activity(session_id)

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
            log.info("Agent %s turn started", session.id)
            self._turn_start_time[session.id] = time.monotonic()
            self._control.cancel_turn_end(session.id)
        elif msg.type == MessageType.TURN_END:
            elapsed = time.monotonic() - self._turn_start_time.pop(session.id, time.monotonic())
            log.info("Agent %s turn ended (%.1fs)", session.id, elapsed)
            self._control.notify_turn_end(session.id)
        elif msg.type == MessageType.STATUS_UPDATE:
            await self._handle_agent_status(session, msg)
        elif msg.type == MessageType.PERMISSION_REQUEST:
            await self._handle_permission_request(session, msg)
        elif msg.type == MessageType.PRIVILEGE_REQUEST:
            await self._handle_privilege_request(session, msg)
        elif msg.type == MessageType.MOUNT_REQUEST:
            await self._handle_mount_request(session, msg)
        elif msg.type == MessageType.FILE_SEND:
            return await self._handle_file_send(session, msg)
        elif msg.type == MessageType.DOWNLOAD_REQUEST:
            return await self._handle_download_request(session, msg)
        elif msg.type == MessageType.SCHEDULE_SET:
            await self._handle_schedule_set(session, msg)
        elif msg.type == MessageType.SCHEDULE_CANCEL:
            await self._handle_schedule_cancel(session, msg)
        elif msg.type == MessageType.TIMER_SET:
            await self._handle_timer_set(session, msg)
        elif msg.type == MessageType.TIMER_CANCEL:
            await self._handle_timer_cancel(session, msg)
        elif msg.type == MessageType.GUI_LAUNCH_REQUEST:
            await self._handle_gui_launch(session, msg)
        elif msg.type == MessageType.SCREENSHOT_REQUEST:
            await self._handle_screenshot(session, msg)
        elif msg.type == MessageType.MEMORY_STORE:
            await self._handle_memory_store(session, msg)
        elif msg.type == MessageType.MEMORY_QUERY:
            await self._handle_memory_query(session, msg)
        elif msg.type == MessageType.MEMORY_LIST:
            await self._handle_memory_list(session, msg)
        elif msg.type == MessageType.MEMORY_DELETE:
            await self._handle_memory_delete(session, msg)
        elif msg.type == MessageType.DREAM_COMPLETE:
            await self._handle_dream_complete(session, msg)
        elif msg.type == MessageType.SUB_AGENT_REQUEST:
            return await self._handle_sub_agent_request(session, msg)
        elif msg.type == MessageType.USAGE_REPORT:
            self._cost.record_usage(
                session_id=session.id,
                input_tokens=msg.payload.get("input_tokens", 0),
                output_tokens=msg.payload.get("output_tokens", 0),
                total_tokens=msg.payload.get("total_tokens", 0),
                model=msg.payload.get("model", ""),
            )
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
        """Handle an intent/thinking/reasoning update — show as status message."""
        thread_id = self._subagent_threads.get(session.id) or self._thread_events.get(session.id)

        # Short intent (e.g. "Exploring the codebase")
        intent = msg.payload.get("intent", "")
        if intent:
            log.debug("Agent %s intent: %s", session.id, intent)
            plain = f"💭 {intent}"
            html = f"💭 <i>{_html_escape(intent)}</i>"
            await self._update_activity(session, plain, thread_id, html=html)
            return

        # Full reasoning text (complete thinking block)
        reasoning = msg.payload.get("reasoning", "")
        if reasoning:
            # Truncate long reasoning to a preview
            preview = reasoning[:200].replace("\n", " ")
            if len(reasoning) > 200:
                preview += "…"
            log.debug("Agent %s reasoning: %s", session.id, preview[:80])
            plain = f"🧠 {preview}"
            html = f"🧠 <i>{_html_escape(preview)}</i>"
            await self._update_activity(session, plain, thread_id, html=html)
            return

        # Streaming reasoning delta — accumulate and show latest chunk
        delta = msg.payload.get("reasoning_delta", "")
        if delta:
            preview = delta.strip()[:150].replace("\n", " ")
            if preview:
                plain = f"🧠 …{preview}"
                html = f"🧠 <i>…{_html_escape(preview)}</i>"
                await self._update_activity(session, plain, thread_id, html=html)

    # Friendly display names for common SDK tools
    _TOOL_LABELS: dict[str, str] = {
        "bash": "Running command",
        "read_bash": "Reading output",
        "write_bash": "Sending input",
        "stop_bash": "Stopping process",
        "view": "Reading file",
        "edit": "Editing file",
        "create": "Creating file",
        "grep": "Searching code",
        "glob": "Finding files",
        "web_fetch": "Fetching URL",
        "web_search": "Searching web",
        "task": "Running sub-agent",
        "read_agent": "Reading agent result",
        "sql": "Running query",
        "ask_user": "Asking user",
        "list_bash": "Listing sessions",
    }

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
        self._audit.log(
            "tool_start", session_id=session.id,
            tool=tool_name, description=description,
        )
        thread_id = self._subagent_threads.get(session.id) or self._thread_events.get(session.id)

        label = self._TOOL_LABELS.get(tool_name, tool_name)
        if description:
            plain = f"🔧 {label}: {description}"
            html = f"🔧 <b>{label}</b>: {_html_escape(description)}"
        else:
            plain = f"🔧 {label}"
            html = f"🔧 <b>{label}</b>"
        await self._update_activity(session, plain, thread_id, html=html)

    async def _handle_tool_complete(
        self, session: Session, msg: Message
    ) -> None:
        """Handle tool execution complete."""
        tool_name = msg.payload.get("tool_name", "unknown")
        success = msg.payload.get("success", True)
        if tool_name in ("report_intent",):
            return
        log.debug("Agent %s tool complete: %s (success=%s)", session.id, tool_name, success)
        self._audit.log(
            "tool_complete", session_id=session.id,
            tool=tool_name, success=success,
        )
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

    async def _handle_sub_agent_request(
        self, session: Session, msg: Message
    ) -> Message:
        """Handle request from agent to spawn a sub-agent.

        Creates a lightweight container with a Matrix thread for the
        sub-agent's activity.  The sub-agent gets its own IPC socket and
        container but shares the parent's Matrix room.
        """
        name = msg.payload.get("name", "sub-agent")
        purpose = msg.payload.get("purpose", "")
        has_network = msg.payload.get("has_network", False)
        has_workspace = msg.payload.get("has_workspace", False)
        profile = msg.payload.get("profile", "light")

        if not purpose:
            return Message(
                type=MessageType.AGENT_RESPONSE,
                payload={"error": "purpose is required"},
                reply_to=msg.id,
            )

        # Limit concurrent sub-agents per session
        max_sub = 3
        active = sum(
            1 for sid, parent in self._subagent_parents.items()
            if parent == session.id
            and self.containers.get_session(sid)
            and self.containers.get_session(sid).status == "running"
        )
        if active >= max_sub:
            return Message(
                type=MessageType.AGENT_RESPONSE,
                payload={"error": f"Max {max_sub} concurrent sub-agents reached"},
                reply_to=msg.id,
            )

        log.info(
            "Spawning sub-agent '%s' for %s: %s",
            name, session.id, purpose[:100],
        )

        # Create a thread in the parent room for the sub-agent
        thread_id = self._thread_events.get(session.id)
        thread_root = await self.matrix.send_message(
            session.room_id,
            f"🤖 **Sub-agent: {name}**\n_{purpose[:200]}_",
            thread_event_id=thread_id,
        )

        # Create IPC socket for sub-agent
        sub_tag = f"sub-{name}-{uuid.uuid4().hex[:6]}"
        socket_path = await self.ipc.create_socket(sub_tag)

        # Resolve profile
        profile_obj = self.containers.config.get_profile(profile)

        # Create a sub-agent session
        sub_session = await self.containers.create_session(
            name=f"sub-{name}",
            room_id=session.room_id,
            socket_path=str(socket_path),
            profile=profile,
            user_display_name=session.user_display_name,
            user_pronouns=session.user_pronouns,
        )

        # Track parent → sub-agent relationship
        self._subagent_parents[sub_session.id] = session.id
        if thread_root:
            self._subagent_threads[sub_session.id] = thread_root

        # Start the container
        started, error = await self.containers.start_session(sub_session.id)
        if not started:
            log.error("Sub-agent container failed to start: %s", error)
            return Message(
                type=MessageType.AGENT_RESPONSE,
                payload={
                    "error": f"Failed to start sub-agent container: {error}",
                    "sub_agent_id": sub_session.id,
                },
                reply_to=msg.id,
            )

        # Send the initial purpose as the first user message to the sub-agent
        await self.ipc.send_to(
            sub_session.id,
            Message(
                type=MessageType.USER_MESSAGE,
                payload={
                    "content": purpose,
                    "sender": "parent-agent",
                    "room_id": session.room_id,
                },
            ),
        )

        log.info(
            "Sub-agent '%s' started: session=%s parent=%s",
            name, sub_session.id, session.id,
        )

        return Message(
            type=MessageType.AGENT_RESPONSE,
            payload={
                "sub_agent_id": sub_session.id,
                "name": name,
                "status": "running",
                "thread_id": thread_root or "",
            },
            reply_to=msg.id,
        )

    async def _update_activity(
        self, session: Session, text: str, thread_id: str | None,
        html: str | None = None,
    ) -> None:
        """Append a line to the activity status message.

        Lines accumulate in a single message (edited in-place) until
        the combined text exceeds _MAX_ACTIVITY_LEN, at which point the
        current message is finalised and a new one is started.  Activity
        messages are **not** deleted — they stay in the chat history.
        """
        lines = self._activity_lines.setdefault(session.id, [])
        lines.append((text, html or text))
        combined_plain = "\n".join(t for t, _ in lines)
        combined_html = "<br/>".join(h for _, h in lines)

        existing = self._activity_msg.get(session.id)

        if existing and len(combined_plain) <= _MAX_ACTIVITY_LEN:
            # Still fits — edit the existing message
            await self.matrix.edit_message(
                session.room_id, existing, combined_plain,
                html_body=combined_html,
            )
        elif existing:
            # Over the limit — finalise current message and start a new one
            self._activity_msg.pop(session.id, None)
            self._activity_lines[session.id] = [(text, html or text)]
            event_id = await self.matrix.send_message(
                session.room_id, text, html_body=html or text,
                thread_event_id=thread_id,
            )
            if event_id:
                self._activity_msg[session.id] = event_id
        else:
            # No existing message — create one
            event_id = await self.matrix.send_message(
                session.room_id, combined_plain, html_body=combined_html,
                thread_event_id=thread_id,
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

        # Notify control socket subscribers
        self._control.notify_response(session.id, content)

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
            ready_msg = f"✅ Agent ready ({mode} mode). Start chatting!"

            # If the room is awaiting a user join, queue the message.
            # Otherwise send immediately (e.g. session restore where user
            # is already in the room).
            if session.room_id in self._awaiting_join:
                self._awaiting_join[session.room_id].append(ready_msg)
            else:
                await self.matrix.send_message(
                    session.room_id, ready_msg,
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
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    },
                )
                sent = await self.ipc.send_to(session.id, flush_msg)
                if not sent:
                    log.warning(
                        "Failed to send queued message to %s", session.id
                    )

        elif status == "compacting":
            log.info("Agent %s: context compaction started", session.id)
            thread_id = self._thread_events.get(session.id)
            await self._update_activity(
                session, "🗜️ Compacting context…", thread_id,
            )

        elif status == "compaction_complete":
            msgs = msg.payload.get("messages_removed", "?")
            tokens = msg.payload.get("tokens_removed", "?")
            pre = msg.payload.get("pre_compaction_tokens")
            post = msg.payload.get("post_compaction_tokens")
            log.info(
                "Agent %s: compaction complete (%s msgs, %s tokens removed, %s → %s)",
                session.id, msgs, tokens, pre, post,
            )
            thread_id = self._thread_events.get(session.id)
            if pre and post:
                detail = f"{int(pre):,} → {int(post):,} tokens ({msgs} messages removed)"
            else:
                detail = f"{msgs} messages, {tokens} tokens freed"
            await self._update_activity(
                session, f"🗜️ Compacted: {detail}", thread_id,
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
        """Handle a permission request from an agent (filesystem/network)."""
        target = msg.payload.get("target", "")
        reason = msg.payload.get("reason", "")
        perm_type_str = msg.payload.get("perm_type", "filesystem")

        try:
            perm_type = PermissionType(perm_type_str)
        except ValueError:
            perm_type = PermissionType.FILESYSTEM

        log.info(
            "Permission request from %s: %s %s (%s)",
            session.id, perm_type.value, target, reason,
        )
        self._audit.log(
            "permission_request", session_id=session.id,
            perm_type=perm_type.value, target=target, reason=reason,
        )

        status, scope, pattern = await self._approval.request_permission(
            session_id=session.id,
            session_name=session.name,
            project_name=session.name,
            perm_type=perm_type,
            target=target,
            reason=reason,
            room_id=session.room_id,
        )

        # If approved with a scope, create a grant
        if status == RequestStatus.APPROVED and scope:
            self._perm_db.add_grant(
                session_id=session.id,
                project_name=session.name,
                perm_type=perm_type,
                target=pattern or target,
                scope=scope,
                granted_by="approval",
                pattern=pattern,
            )
            self._audit.log(
                "permission_granted", session_id=session.id,
                perm_type=perm_type.value, target=pattern or target,
                scope=scope.value,
            )
        elif status != RequestStatus.APPROVED:
            self._audit.log(
                "permission_denied", session_id=session.id,
                perm_type=perm_type.value, target=target,
            )

        # Send response back to agent
        response = Message(
            type=MessageType.PERMISSION_RESPONSE,
            payload={
                "approved": status == RequestStatus.APPROVED,
                "scope": scope.value if scope else None,
                "target": target,
            },
            reply_to=msg.id,
        )
        await self.ipc.send_to(session.id, response)

    async def _handle_privilege_request(
        self, session: Session, msg: Message
    ) -> None:
        """Handle a privilege escalation request from an agent.

        Flow: check DB → post poll in project room → wait for vote →
        if approved, execute via priv broker → return result.
        """
        command = msg.payload.get("command", "")
        args = msg.payload.get("args", [])
        reason = msg.payload.get("reason", "")
        suggested_pattern = msg.payload.get("suggested_pattern")
        full_cmd = f"{command} {' '.join(args)}".strip() if args else command

        log.info(
            "Privilege request from %s: %s (%s)",
            session.id, full_cmd, reason,
        )
        self._audit.log(
            "privilege_request", session_id=session.id,
            command=full_cmd, reason=reason,
        )

        # Fast reject if privilege broker is not available (user has no sudo)
        if not self._priv_client.is_connected:
            if not await self._priv_client.connect():
                log.info("Auto-rejecting privilege request — no priv broker")
                response = Message(
                    type=MessageType.PRIVILEGE_RESPONSE,
                    payload={
                        "approved": False,
                        "command": command,
                        "error": (
                            "Sudo is not available — the host user does not have "
                            "privilege escalation configured. The privilege broker "
                            "is not running."
                        ),
                    },
                    reply_to=msg.id,
                )
                await self.ipc.send_to(session.id, response)
                return

        # Ask for approval via poll in the project room
        status, scope, pattern = await self._approval.request_permission(
            session_id=session.id,
            session_name=session.name,
            project_name=session.name,
            perm_type=PermissionType.PRIVILEGE,
            target=full_cmd,
            reason=reason,
            room_id=session.room_id,
            suggested_pattern=suggested_pattern,
        )

        if status != RequestStatus.APPROVED:
            self._audit.log(
                "privilege_denied", session_id=session.id, command=full_cmd,
            )
            response = Message(
                type=MessageType.PRIVILEGE_RESPONSE,
                payload={
                    "approved": False,
                    "command": command,
                    "error": f"Request {status.value}",
                },
                reply_to=msg.id,
            )
            await self.ipc.send_to(session.id, response)
            return

        # If scope was set, create a grant for future requests
        if scope:
            self._perm_db.add_grant(
                session_id=session.id,
                project_name=session.name,
                perm_type=PermissionType.PRIVILEGE,
                target=pattern or full_cmd,
                scope=scope,
                granted_by="approval",
                pattern=pattern,
            )
            self._audit.log(
                "privilege_granted", session_id=session.id,
                command=full_cmd, scope=scope.value,
            )

        # Execute via privilege broker
        if not self._priv_client.is_connected:
            if not await self._priv_client.connect():
                response = Message(
                    type=MessageType.PRIVILEGE_RESPONSE,
                    payload={
                        "approved": True,
                        "command": command,
                        "error": "Privilege broker not available",
                        "stdout": "",
                        "stderr": "",
                        "exit_code": -1,
                    },
                    reply_to=msg.id,
                )
                await self.ipc.send_to(session.id, response)
                return

        result = await self._priv_client.exec_command(
            session_id=session.id,
            command=command,
            args=args,
        )
        self._audit.log(
            "privilege_executed", session_id=session.id,
            command=full_cmd, exit_code=result.exit_code,
            success=result.success,
        )

        response = Message(
            type=MessageType.PRIVILEGE_RESPONSE,
            payload={
                "approved": True,
                "command": command,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "exit_code": result.exit_code,
                "error": result.error if not result.success else "",
            },
            reply_to=msg.id,
        )
        await self.ipc.send_to(session.id, response)

    async def _handle_mount_request(
        self, session: Session, msg: Message
    ) -> None:
        """Handle a dynamic mount request from an agent.

        Flow: post approval poll → if approved, set up propagation (if needed)
        → bind-mount source into workspace via priv broker → agent sees it at
        /workspace/<mount_name>.
        """
        source_path = msg.payload.get("source_path", "")
        reason = msg.payload.get("reason", "")
        suggested_pattern = msg.payload.get("suggested_pattern")

        log.info(
            "Mount request from %s: %s (%s)",
            session.id, source_path, reason,
        )
        self._audit.log(
            "mount_request", session_id=session.id,
            source_path=source_path, reason=reason,
        )

        # Ask for approval via poll in the project room
        status, scope, pattern = await self._approval.request_permission(
            session_id=session.id,
            session_name=session.name,
            project_name=session.name,
            perm_type=PermissionType.FILESYSTEM,
            target=f"mount:{source_path}",
            reason=reason,
            room_id=session.room_id,
            suggested_pattern=suggested_pattern,
        )

        if status != RequestStatus.APPROVED:
            response = Message(
                type=MessageType.MOUNT_RESPONSE,
                payload={
                    "approved": False,
                    "source_path": source_path,
                    "error": f"Request {status.value}",
                },
                reply_to=msg.id,
            )
            await self.ipc.send_to(session.id, response)
            return

        # Record grant if scoped
        if scope:
            self._perm_db.add_grant(
                session_id=session.id,
                project_name=session.name,
                perm_type=PermissionType.FILESYSTEM,
                target=pattern or f"mount:{source_path}",
                scope=scope,
                granted_by="approval",
                pattern=pattern,
            )

        # Ensure priv broker is connected
        if not self._priv_client.is_connected:
            if not await self._priv_client.connect():
                response = Message(
                    type=MessageType.MOUNT_RESPONSE,
                    payload={
                        "approved": True,
                        "source_path": source_path,
                        "error": "Privilege broker not available",
                    },
                    reply_to=msg.id,
                )
                await self.ipc.send_to(session.id, response)
                return

        workspace = session.workspace_path

        # Set up shared mount propagation on workspace (once per workspace)
        if workspace not in self._propagation_ready:
            result = await self._priv_client.make_shared(
                session_id=session.id, path=workspace,
            )
            if result.success:
                self._propagation_ready.add(workspace)
                log.info("Shared propagation set up: %s", workspace)
            else:
                log.warning(
                    "Failed to set up propagation for %s: %s",
                    workspace, result.error,
                )
                # Continue anyway — mount may still work if propagation was
                # already set up from a previous run

        # Create mount name from source path
        mount_name = (
            source_path.strip("/")
            .replace("/", "-")
            .replace(" ", "-")
            .replace("..", "")[:64]
        )
        target = f"{workspace}/{mount_name}"

        # Bind-mount via priv broker (runs as root)
        result = await self._priv_client.mount(
            session_id=session.id,
            source=source_path,
            target=target,
        )

        if result.success:
            container_path = f"/workspace/{mount_name}"
            response = Message(
                type=MessageType.MOUNT_RESPONSE,
                payload={
                    "approved": True,
                    "source_path": source_path,
                    "container_path": container_path,
                    "mount_name": mount_name,
                },
                reply_to=msg.id,
            )
            await self.matrix.send_message(
                session.room_id,
                f"📂 Mounted `{source_path}` → `{container_path}`",
            )
        else:
            response = Message(
                type=MessageType.MOUNT_RESPONSE,
                payload={
                    "approved": True,
                    "source_path": source_path,
                    "error": result.error or "Mount failed",
                },
                reply_to=msg.id,
            )

        await self.ipc.send_to(session.id, response)

    # ------------------------------------------------------------------
    # Matrix poll/reaction → approval flow
    # ------------------------------------------------------------------

    async def _on_poll_response(
        self, room_id: str, sender: str, poll_event_id: str, answer_ids: list[str]
    ) -> None:
        """Handle a poll response — check generic polls, then approval."""
        # Check generic polls first (e.g., profile selection)
        generic = self._generic_polls.get(poll_event_id)
        if generic is not None:
            event, result = generic
            if answer_ids:
                result.append(answer_ids[0])
            event.set()
            return

        request_id, scope, needs_pattern = self._approval.handle_poll_response(
            poll_event_id=poll_event_id,
            answer_ids=answer_ids,
            sender=sender,
            room_id=room_id,
        )
        if request_id is None:
            return

        if needs_pattern:
            # Ask user for custom pattern
            await self.matrix.send_message(
                room_id,
                "✏️ Enter your custom regex pattern for this approval:",
            )
            log.info("Awaiting custom pattern for request %s", request_id)
        else:
            action = "denied" if scope is None else f"approved ({scope.value})"
            log.info(
                "Poll approval from %s: request %s %s",
                sender, request_id, action,
            )

    async def _on_matrix_reaction(
        self, room_id: str, sender: str, reacts_to: str, emoji: str
    ) -> None:
        """Handle a reaction from Matrix (legacy, kept for backwards compat)."""
        request_id, scope = self._approval.handle_reaction(
            event_id=reacts_to,
            emoji=emoji,
            sender=sender,
        )
        if request_id is not None:
            action = "denied" if scope is None else f"approved ({scope.value})"
            log.info(
                "Approval reaction from %s: request %s %s",
                sender, request_id, action,
            )

    # ------------------------------------------------------------------
    # Permission management commands (!perms, !revoke)
    # ------------------------------------------------------------------

    async def _cmd_perms(self, cmd: ParsedCommand) -> None:
        """List active permissions / grants."""
        project = cmd.raw_args.strip() if cmd.raw_args.strip() else None
        grants = self._perm_db.list_grants(project_name=project)
        if not grants:
            await self._reply_control("No active permissions.")
            return

        lines = ["**Active Permissions**\n"]
        for g in grants[:20]:
            status = "✅" if g.is_active else "❌"
            lines.append(
                f"{status} `{g.id}` **{g.perm_type.value}** `{g.target}` "
                f"({g.scope.value}) — {g.project_name}"
            )
        await self._reply_control("\n".join(lines))

    async def _cmd_revoke(self, cmd: ParsedCommand) -> None:
        """Revoke a permission grant by ID."""
        grant_id = cmd.raw_args.strip()
        if not grant_id:
            await self._reply_control("Usage: `revoke <grant-id>`")
            return
        try:
            gid = int(grant_id)
        except ValueError:
            await self._reply_control(f"Invalid grant ID: `{grant_id}`")
            return
        self._perm_db.revoke_grant(gid, revoked_by="control")
        await self._reply_control(f"✅ Revoked grant `{gid}`.")

    # ------------------------------------------------------------------
    # Mount propagation
    # ------------------------------------------------------------------

    async def _ensure_propagation(self, session: Session) -> None:
        """Set up shared mount propagation on a session's workspace.

        This makes it so bind-mounts added to the workspace directory on the
        host are visible inside the running container. Must be called before
        container start and requires the priv broker.
        """
        workspace = session.workspace_path
        if workspace in self._propagation_ready:
            return

        if not self._priv_client.is_connected:
            if not await self._priv_client.connect():
                log.warning("Priv broker unavailable — skipping propagation setup")
                return

        result = await self._priv_client.make_shared(
            session_id=session.id, path=workspace,
        )
        if result.success:
            self._propagation_ready.add(workspace)
            log.info("Shared propagation set up: %s", workspace)
        else:
            log.warning(
                "Failed to set up propagation for %s: %s",
                workspace, result.error,
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

            # Set up shared propagation before starting container
            await self._ensure_propagation(session)

            started, error = await self.containers.start_session(session.id)
            if started:
                log.info("Session restored: %s", session.id)
            else:
                await self.matrix.send_message(
                    room_id, f"❌ Failed to start container: {error}"
                )
        finally:
            self._restoring.discard(session.id)

    # ------------------------------------------------------------------
    # Agent connect/disconnect
    # ------------------------------------------------------------------

    async def _on_agent_connect(self, session_id: str) -> None:
        """Called when an agent connects via IPC."""
        log.info("Agent connected: %s", session_id)
        self._audit.log("agent_connected", session_id=session_id)
        # Start file watcher for workspace changes
        await self._start_watcher(session_id)

    async def _on_agent_disconnect(self, session_id: str) -> None:
        """Called when an agent disconnects."""
        # Clean up streaming state
        self._streaming.pop(session_id, None)

        # Stop file watcher
        await self._stop_watcher(session_id)

        session = self.containers.get_session(session_id)
        if session:
            if session.status == "stopping":
                await self.matrix.send_message(
                    session.room_id,
                    "🛑 Session stopped.",
                )
            elif session.status == "running":
                await self.matrix.send_message(
                    session.room_id,
                    "⚠️ Agent disconnected unexpectedly.",
                )
        log.info("Agent disconnected: %s", session_id)
        self._audit.log("agent_disconnected", session_id=session_id)

    async def _start_watcher(self, session_id: str) -> None:
        """Start a file watcher for a session's workspace."""
        if session_id in self._watchers:
            return

        session = self.containers.get_session(session_id)
        if not session or not session.workspace_path:
            return

        async def on_changes(changes: list[dict]) -> None:
            """Deliver file change notifications to the agent."""
            if not self.ipc.is_connected(session_id):
                return
            self._touch_activity(session_id)
            await self.ipc.send_to(session_id, Message(
                type=MessageType.FILE_CHANGE,
                payload={
                    "changes": changes,
                    "count": len(changes),
                },
            ))

        watcher = WorkspaceWatcher(
            workspace_path=session.workspace_path,
            on_changes=on_changes,
        )
        await watcher.start()
        self._watchers[session_id] = watcher
        log.info("Started file watcher for %s", session_id)

    async def _stop_watcher(self, session_id: str) -> None:
        """Stop a file watcher for a session."""
        watcher = self._watchers.pop(session_id, None)
        if watcher:
            await watcher.stop()
            log.info("Stopped file watcher for %s", session_id)

    async def _on_user_join(self, room_id: str, user_id: str) -> None:
        """Called when a user joins a room — flush queued messages."""
        queued = self._awaiting_join.pop(room_id, None)
        if queued is None:
            return
        log.info("User %s joined %s, sending %d queued messages", user_id, room_id, len(queued))
        for msg in queued:
            await self.matrix.send_message(room_id, msg)

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

    async def _cmd_project_with_cleanup(
        self, sender: str, cmd: ParsedCommand,
        event_id: str | None, thinking_eid: str | None,
    ) -> None:
        """Run _cmd_project as a background task with its own UI cleanup."""
        try:
            await self._cmd_project(sender, cmd)
        except Exception as e:
            log.error("Project command failed: %s", e)
            await self._reply_control(f"❌ Project creation failed: {e}")
        finally:
            await self.matrix.set_typing(self.control_room_id, False)
            if thinking_eid:
                await self.matrix.redact_event(
                    self.control_room_id, thinking_eid
                )
            if event_id:
                await self.matrix.send_reaction(
                    self.control_room_id, event_id, "✅"
                )

    async def _cmd_project(self, sender: str, cmd: ParsedCommand) -> None:
        """Handle the project command — create a new project session.

        Syntax: project <name> [profile]
        If the last word matches a known profile, it's used as the profile.
        Otherwise the user is asked to pick a profile via a poll.
        """
        if not cmd.has_args:
            profiles = self.containers.config.profiles
            default = self.containers.config.default_profile
            lines = ["Usage: `project <name> [profile]`\n**Profiles:**"]
            for name, prof in profiles.items():
                label = prof.description or name
                marker = " *(default)*" if name == default else ""
                lines.append(f"- `{name}` — {label}{marker}")
            await self._reply_control("\n".join(lines))
            return

        raw = cmd.args[0]  # single-arg: full string like "myapp light"
        profile_name = ""
        project_name = raw

        # Check if the last word is a known profile name
        parts = raw.rsplit(None, 1)
        if len(parts) == 2:
            maybe_name, maybe_profile = parts
            if maybe_profile in self.containers.config.profiles:
                project_name = maybe_name
                profile_name = maybe_profile

        # Guard against duplicate creation (e.g. from event re-delivery)
        if project_name in self._creating_projects:
            log.warning("Already creating project %s, skipping duplicate", project_name)
            return
        self._creating_projects.add(project_name)

        try:
            # If no profile specified, ask the user via poll
            if not profile_name:
                profile_name = await self._ask_profile(project_name)
                if not profile_name:
                    await self._reply_control(
                        f"⏱️ Profile selection timed out for **{project_name}**."
                    )
                    return

            log.info("[project:%s] Profile selected: %s — starting setup...", project_name, profile_name)
            await self._reply_control(
                f"⚙️ Setting up **{project_name}** with profile `{profile_name}`..."
            )

            await self._cmd_project_inner(sender, project_name, profile_name)
        finally:
            self._creating_projects.discard(project_name)

    async def _ask_profile(self, project_name: str) -> str | None:
        """Send a profile selection poll and wait for the user's choice.

        Returns the selected profile name, or None on timeout.
        """
        profiles = self.containers.config.profiles

        # Skip poll if only one profile exists
        if len(profiles) == 1:
            return next(iter(profiles))

        default = self.containers.config.default_profile

        # Build poll answers — default first, using description as label
        answers: list[tuple[str, str]] = []
        # Put default first
        if default in profiles:
            prof = profiles[default]
            label = prof.description or default
            answers.append((default, label))
        for name, prof in profiles.items():
            if name == default:
                continue
            label = prof.description or name
            answers.append((name, label))

        question = f"🏰 Choose a profile for **{project_name}**:"

        poll_event_id = await self.matrix.send_poll(
            self.control_room_id, question, answers
        )
        if poll_event_id is None:
            # Fall back to default if poll send fails
            return default

        # Register and wait for response
        wait_event = asyncio.Event()
        result: list[str] = []
        self._generic_polls[poll_event_id] = (wait_event, result)

        try:
            await asyncio.wait_for(wait_event.wait(), timeout=120.0)
        except asyncio.TimeoutError:
            return None
        finally:
            self._generic_polls.pop(poll_event_id, None)
            # Close the poll
            try:
                await self.matrix.end_poll(self.control_room_id, poll_event_id)
            except Exception:
                pass

        if result and result[0] in profiles:
            return result[0]
        return default

    async def _cmd_project_inner(
        self, sender: str, project_name: str, profile: str = ""
    ) -> None:
        """Inner implementation of project creation."""
        resolved_profile = profile or self.containers.config.default_profile
        profile_obj = self.containers.config.get_profile(resolved_profile)

        log.info("[project:%s] Creating room (profile=%s)...", project_name, resolved_profile)
        room_id = await self.matrix.create_room(
            name=f"🏰 {project_name}",
            topic=f"Enclave project: {project_name} [{resolved_profile}]",
            encrypted=True,
            space_id=self.space_id,
        )

        if room_id is None:
            await self._reply_control(
                f"❌ Failed to create room for **{project_name}**."
            )
            return

        log.info("[project:%s] Room created: %s", project_name, room_id)

        log.info("[project:%s] Creating IPC socket...", project_name)
        socket_path = await self.ipc.create_socket(f"pending-{project_name}")

        # Look up user identity for personalization
        user = self._user_mappings.get(sender)
        user_display_name = user.display_name if user else ""
        user_pronouns = user.pronouns if user else ""

        session = await self.containers.create_session(
            name=project_name,
            room_id=room_id,
            socket_path=str(socket_path),
            profile=resolved_profile,
            user_display_name=user_display_name,
            user_pronouns=user_pronouns,
        )
        log.info("[project:%s] Session created: %s", project_name, session.id)
        self._audit.log(
            "session_created", session_id=session.id, user=sender,
            name=project_name, profile=resolved_profile,
            room_id=room_id,
        )

        # Write key memories to workspace for agent to read
        if sender:
            store = self._get_memory_store(sender)
            if store:
                prompt = store.key_memories_as_prompt(
                    max_lines=self._memory_config.key_memory_limit
                    if self._memory_config else 200
                )
                if prompt:
                    mem_path = Path(session.workspace_path) / ".enclave-memories"
                    mem_path.write_text(prompt)
                    log.info(
                        "[project:%s] Wrote %d key memories to workspace",
                        project_name, len(store.list_key_memories()),
                    )

        await self.ipc.remove_socket(f"pending-{project_name}")
        socket_path = await self.ipc.create_socket(session.id)
        session.socket_path = str(socket_path)

        # Set up shared propagation before starting container
        log.info("[project:%s] Setting up mount propagation...", project_name)
        await self._ensure_propagation(session)
        log.info("[project:%s] Mount propagation done", project_name)

        log.info("[project:%s] Starting container...", project_name)
        started, error = await self.containers.start_session(session.id)
        log.info("[project:%s] Container start result: %s", project_name, started)

        # Mark room as awaiting user join — messages will be queued
        # until the user actually joins the room
        self._awaiting_join[room_id] = []

        # Invite the user only after the container is ready
        log.info("[project:%s] Inviting user %s...", project_name, sender)
        await self.matrix.invite_user(room_id, sender)
        await self.matrix._trust_users([sender])
        log.info("[project:%s] Setup complete", project_name)

        if started:
            await self._reply_control(
                f"✅ Project **{project_name}** created!\n"
                f"Profile: `{resolved_profile}` → `{profile_obj.image}`\n"
                f"Session ID: `{session.id}`"
            )
        else:
            await self._reply_control(
                f"⚠️ Room created for **{project_name}** but container "
                f"failed to start: {error}\n"
                f"Session ID: `{session.id}`"
            )

    # ------------------------------------------------------------------
    # Scheduler IPC handlers
    # ------------------------------------------------------------------

    async def _handle_schedule_set(self, session: Session, msg: Message) -> None:
        """Handle a SCHEDULE_SET request from an agent."""
        interval = msg.payload.get("interval_seconds", 0)
        reason = msg.payload.get("reason", "")
        schedule_id = msg.payload.get("id", f"sched-{session.id}-{int(time.time())}")

        result = self._scheduler.add_schedule(
            schedule_id=schedule_id,
            session_id=session.id,
            interval_seconds=interval,
            reason=reason,
        )

        if isinstance(result, str):
            # Error
            await self.ipc.send_to(session.id, Message(
                type=MessageType.SCHEDULE_TRIGGER,
                payload={"error": result, "id": schedule_id},
                reply_to=msg.id,
            ))
        else:
            await self.ipc.send_to(session.id, Message(
                type=MessageType.SCHEDULE_TRIGGER,
                payload={
                    "ok": True,
                    "id": schedule_id,
                    "next_fire": result.next_fire,
                },
                reply_to=msg.id,
            ))

    async def _handle_schedule_cancel(self, session: Session, msg: Message) -> None:
        """Handle a SCHEDULE_CANCEL request from an agent."""
        schedule_id = msg.payload.get("id", "")
        found = self._scheduler.cancel_schedule(schedule_id)
        await self.ipc.send_to(session.id, Message(
            type=MessageType.SCHEDULE_TRIGGER,
            payload={"ok": found, "id": schedule_id, "cancelled": True},
            reply_to=msg.id,
        ))

    async def _handle_timer_set(self, session: Session, msg: Message) -> None:
        """Handle a TIMER_SET request from an agent."""
        fire_at = msg.payload.get("fire_at", 0)
        delay_seconds = msg.payload.get("delay_seconds", 0)
        reason = msg.payload.get("reason", "")
        timer_id = msg.payload.get("id", f"timer-{session.id}-{int(time.time())}")

        # Support both absolute time and relative delay
        if delay_seconds > 0 and not fire_at:
            fire_at = time.time() + delay_seconds

        result = self._scheduler.add_timer(
            timer_id=timer_id,
            session_id=session.id,
            fire_at=fire_at,
            reason=reason,
        )

        if isinstance(result, str):
            await self.ipc.send_to(session.id, Message(
                type=MessageType.TIMER_TRIGGER,
                payload={"error": result, "id": timer_id},
                reply_to=msg.id,
            ))
        else:
            await self.ipc.send_to(session.id, Message(
                type=MessageType.TIMER_TRIGGER,
                payload={
                    "ok": True,
                    "id": timer_id,
                    "fire_at": result.fire_at,
                },
                reply_to=msg.id,
            ))

    async def _handle_timer_cancel(self, session: Session, msg: Message) -> None:
        """Handle a TIMER_CANCEL request from an agent."""
        timer_id = msg.payload.get("id", "")
        found = self._scheduler.cancel_timer(timer_id)
        await self.ipc.send_to(session.id, Message(
            type=MessageType.TIMER_TRIGGER,
            payload={"ok": found, "id": timer_id, "cancelled": True},
            reply_to=msg.id,
        ))

    async def _on_schedule_fire(
        self, session_id: str, entry: ScheduleEntry,
    ) -> None:
        """Called when a recurring schedule fires — send message to agent."""
        if not self.ipc.is_connected(session_id):
            log.info("Schedule %s: session %s not connected, skipping", entry.id, session_id)
            return
        await self.ipc.send_to(session_id, Message(
            type=MessageType.SCHEDULE_TRIGGER,
            payload={
                "id": entry.id,
                "reason": entry.reason,
                "fired": True,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        ))

    async def _on_timer_fire(
        self, session_id: str, entry: TimerEntry,
    ) -> None:
        """Called when a one-shot timer fires — send message to agent."""
        session = self.containers.get_session(session_id)
        if not session:
            log.info("Timer %s: session %s gone, skipping", entry.id, session_id)
            return

        # If agent not connected, try to restore the session first
        if not self.ipc.is_connected(session_id):
            log.info("Timer %s: session %s not connected, restoring...", entry.id, session_id)
            await self._restore_session(session, session.room_id)
            # Wait a bit for the agent to connect
            for _ in range(30):
                await asyncio.sleep(1)
                if self.ipc.is_connected(session_id):
                    break
            else:
                log.warning("Timer %s: session %s failed to restore", entry.id, session_id)
                return

        await self.ipc.send_to(session_id, Message(
            type=MessageType.TIMER_TRIGGER,
            payload={
                "id": entry.id,
                "reason": entry.reason,
                "fired": True,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        ))

    # ------------------------------------------------------------------
    # Display/UI IPC handlers
    # ------------------------------------------------------------------

    async def _handle_gui_launch(self, session: Session, msg: Message) -> None:
        """Handle a GUI launch request — requires approval."""
        command = msg.payload.get("command", "")
        reason = msg.payload.get("reason", "")

        if not self._display.is_available:
            await self.ipc.send_to(session.id, Message(
                type=MessageType.GUI_LAUNCH_REQUEST,
                payload={"error": "No desktop session available"},
                reply_to=msg.id,
            ))
            return

        # Translate container paths to host paths
        extra_env: dict[str, str] = {}
        if "/workspace" in command and session.workspace_path:
            command = command.replace("/workspace", session.workspace_path)
            log.debug("Translated GUI command path: %s", command)

            # Container-built binaries may need libraries from the container's
            # nix store or system libs.  Resolve missing shared libs on the host.
            extra_lib_dirs = await self._resolve_missing_libs(command)
            if extra_lib_dirs:
                existing = os.environ.get("LD_LIBRARY_PATH", "")
                extra_env["LD_LIBRARY_PATH"] = ":".join(extra_lib_dirs) + (
                    f":{existing}" if existing else ""
                )
                log.info("Added LD_LIBRARY_PATH for GUI: %s", extra_env["LD_LIBRARY_PATH"])

        # Require approval like sudo
        status, scope, pattern = await self._approval.request_permission(
            session_id=session.id,
            session_name=session.name,
            project_name=session.name,
            perm_type=PermissionType.PRIVILEGE,
            target=f"GUI: {command}",
            reason=reason or f"Launch GUI application: {command}",
            room_id=session.room_id,
        )

        if status != RequestStatus.APPROVED:
            await self.ipc.send_to(session.id, Message(
                type=MessageType.GUI_LAUNCH_REQUEST,
                payload={"error": f"GUI launch denied: {status.value}", "command": command},
                reply_to=msg.id,
            ))
            return

        success = await self._display.launch_app(command, extra_env=extra_env)
        await self.ipc.send_to(session.id, Message(
            type=MessageType.GUI_LAUNCH_REQUEST,
            payload={"ok": success, "command": command},
            reply_to=msg.id,
        ))

    async def _resolve_missing_libs(self, command: str) -> list[str]:
        """Find host nix-store library dirs for missing shared libraries.

        Runs ldd on the binary, parses 'not found' lines, then locates
        the libs in /nix/store. Returns a list of directories to add to
        LD_LIBRARY_PATH.
        """
        # Extract the binary path (first token of command)
        binary = command.split()[0]
        if not os.path.isfile(binary):
            return []

        try:
            proc = await asyncio.create_subprocess_exec(
                "ldd", binary,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            missing = []
            for line in stdout.decode(errors="replace").splitlines():
                if "not found" in line:
                    # Format: "\tlibFoo.so.0 => not found"
                    lib_name = line.strip().split()[0]
                    missing.append(lib_name)

            if not missing:
                return []

            log.info("Missing libs for %s: %s", binary, missing)

            # Search nix store for each missing lib
            lib_dirs: list[str] = []
            for lib in missing:
                find_proc = await asyncio.create_subprocess_exec(
                    "find", "/nix/store", "-maxdepth", "3",
                    "-name", lib, "-type", "f",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                out, _ = await asyncio.wait_for(find_proc.communicate(), timeout=10.0)
                paths = out.decode(errors="replace").strip().splitlines()
                if paths:
                    # Use the last (newest) match
                    lib_dir = str(Path(paths[-1]).parent)
                    if lib_dir not in lib_dirs:
                        lib_dirs.append(lib_dir)
                        log.debug("Found %s at %s", lib, lib_dir)

            return lib_dirs
        except Exception as e:
            log.warning("Failed to resolve missing libs: %s", e)
            return []

    async def _handle_screenshot(self, session: Session, msg: Message) -> None:
        """Handle a screenshot request — auto-approved (read-only)."""
        if not self._display.is_available:
            await self.ipc.send_to(session.id, Message(
                type=MessageType.SCREENSHOT_REQUEST,
                payload={"error": "No desktop session available"},
                reply_to=msg.id,
            ))
            return

        # Save to session workspace
        output_path = str(
            Path(session.workspace_path) / f"screenshot-{int(time.time())}.png"
        )
        success = await self._display.take_screenshot(output_path)

        if success:
            await self.ipc.send_to(session.id, Message(
                type=MessageType.SCREENSHOT_REQUEST,
                payload={"ok": True, "path": output_path},
                reply_to=msg.id,
            ))
        else:
            await self.ipc.send_to(session.id, Message(
                type=MessageType.SCREENSHOT_REQUEST,
                payload={"error": "Screenshot failed"},
                reply_to=msg.id,
            ))

    # ------------------------------------------------------------------
    # Memory IPC handlers
    # ------------------------------------------------------------------

    async def _handle_memory_store(self, session: Session, msg: Message) -> None:
        """Store a memory from an agent."""
        user_id = self._get_user_for_session(session)
        store = self._get_memory_store(user_id)
        if not store:
            await self.ipc.send_to(session.id, Message(
                type=MessageType.MEMORY_STORE,
                payload={"error": "Memory not enabled"},
                reply_to=msg.id,
            ))
            return

        content = msg.payload.get("content", "")
        category = msg.payload.get("category", "other")
        is_key = msg.payload.get("is_key", False)

        if not content:
            await self.ipc.send_to(session.id, Message(
                type=MessageType.MEMORY_STORE,
                payload={"error": "Empty content"},
                reply_to=msg.id,
            ))
            return

        mem = store.store(
            content=content,
            category=category,
            source_session=session.id,
            is_key_memory=is_key,
        )
        await self.ipc.send_to(session.id, Message(
            type=MessageType.MEMORY_STORE,
            payload={"ok": True, "memory": mem.to_dict()},
            reply_to=msg.id,
        ))

    async def _handle_memory_query(self, session: Session, msg: Message) -> None:
        """Search memories for an agent."""
        user_id = self._get_user_for_session(session)
        store = self._get_memory_store(user_id)
        if not store:
            await self.ipc.send_to(session.id, Message(
                type=MessageType.MEMORY_QUERY,
                payload={"error": "Memory not enabled"},
                reply_to=msg.id,
            ))
            return

        keyword = msg.payload.get("keyword", "")
        category = msg.payload.get("category", "")
        limit = msg.payload.get("limit", 20)

        results = store.query(keyword=keyword, category=category, limit=limit)
        await self.ipc.send_to(session.id, Message(
            type=MessageType.MEMORY_QUERY,
            payload={
                "ok": True,
                "memories": [m.to_dict() for m in results],
                "count": len(results),
            },
            reply_to=msg.id,
        ))

    async def _handle_memory_list(self, session: Session, msg: Message) -> None:
        """List key or recent memories."""
        user_id = self._get_user_for_session(session)
        store = self._get_memory_store(user_id)
        if not store:
            await self.ipc.send_to(session.id, Message(
                type=MessageType.MEMORY_LIST,
                payload={"error": "Memory not enabled"},
                reply_to=msg.id,
            ))
            return

        mode = msg.payload.get("mode", "key")  # "key" or "recent"
        if mode == "key":
            results = store.list_key_memories()
        else:
            limit = msg.payload.get("limit", 20)
            results = store.list_recent(limit=limit)

        await self.ipc.send_to(session.id, Message(
            type=MessageType.MEMORY_LIST,
            payload={
                "ok": True,
                "memories": [m.to_dict() for m in results],
                "count": len(results),
            },
            reply_to=msg.id,
        ))

    async def _handle_memory_delete(self, session: Session, msg: Message) -> None:
        """Delete a memory."""
        user_id = self._get_user_for_session(session)
        store = self._get_memory_store(user_id)
        if not store:
            await self.ipc.send_to(session.id, Message(
                type=MessageType.MEMORY_DELETE,
                payload={"error": "Memory not enabled"},
                reply_to=msg.id,
            ))
            return

        memory_id = msg.payload.get("memory_id", "")
        deleted = store.delete(memory_id)
        await self.ipc.send_to(session.id, Message(
            type=MessageType.MEMORY_DELETE,
            payload={"ok": deleted, "memory_id": memory_id},
            reply_to=msg.id,
        ))

    async def _handle_dream_complete(self, session: Session, msg: Message) -> None:
        """Store memories extracted by auto-dreaming."""
        user_id = self._get_user_for_session(session)
        store = self._get_memory_store(user_id)
        if not store:
            return

        extracted = msg.payload.get("memories", [])
        if extracted:
            stored = store.store_from_dreaming(extracted, source_session=session.id)
            log.info(
                "Dream complete for %s: %d new memories stored",
                session.id, stored,
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
            profile_tag = f" `[{s.profile}]`" if s.profile else ""
            lines.append(
                f"  {connected} **{s.name}**{profile_tag} — `{s.id}` ({s.status})"
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
        self._audit.log("session_killed", session_id=session_id)

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

    async def _cmd_cleanup(self, cmd: ParsedCommand) -> None:
        """Clean up Matrix rooms for stopped sessions.

        Usage:
            cleanup           — list stopped sessions eligible for cleanup
            cleanup all       — clean up all stopped sessions
            cleanup <id>      — clean up a specific stopped session
        """
        sessions = self.containers.list_sessions()
        stopped = [s for s in sessions if s.status == "stopped"]

        if not cmd.has_args:
            if not stopped:
                await self._reply_control("No stopped sessions to clean up.")
                return
            lines = [f"**Stopped sessions ({len(stopped)}):**\n"]
            for s in stopped:
                lines.append(f"  `{s.id}` — {s.name} (room: `{s.room_id}`)")
            lines.append(f"\nUse `cleanup all` or `cleanup <session-id>` to remove.")
            await self._reply_control("\n".join(lines))
            return

        target = cmd.args[0]

        if target == "all":
            cleaned = 0
            for s in stopped:
                ok = await self._cleanup_session_room(s)
                if ok:
                    cleaned += 1
            await self._reply_control(
                f"🧹 Cleaned up {cleaned}/{len(stopped)} stopped sessions."
            )
        else:
            session = self.containers.get_session(target)
            if session is None:
                await self._reply_control(f"❌ Session `{target}` not found.")
                return
            if session.status == "running":
                await self._reply_control(
                    f"⚠️ Session `{target}` is still running. Stop it first with `kill {target}`."
                )
                return
            ok = await self._cleanup_session_room(session)
            if ok:
                await self._reply_control(f"🧹 Cleaned up session `{target}`.")
            else:
                await self._reply_control(f"❌ Failed to clean up session `{target}`.")

    async def _cleanup_session_room(self, session: Session) -> bool:
        """Clean up a single stopped session: kick users, leave room, remove session."""
        # Find which users are in this room
        room_id = session.room_id
        reason = f"Session '{session.name}' archived by Enclave"

        # Kick all users we know about
        user_ids = list(self._user_mappings.keys())
        ok = await self.matrix.cleanup_room(room_id, user_ids=user_ids, reason=reason)

        # Remove session from container manager
        await self.containers.remove_session(session.id)
        await self.ipc.remove_socket(session.id)

        log.info("Cleaned up session %s (room: %s)", session.id, room_id)
        return ok
