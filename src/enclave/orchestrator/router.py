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
from enclave.orchestrator.session_manager import SessionManager
from enclave.orchestrator.ipc import IPCServer
from enclave.orchestrator.matrix_client import EnclaveMatrixClient
from enclave.orchestrator.permissions import (
    PermissionDB,
    PermissionScope,
    PermissionType,
    RequestStatus,
)
from enclave.orchestrator.scheduler import Scheduler, ScheduleEntry, TimerEntry
from enclave.orchestrator.display import DisplayManager
from enclave.orchestrator.memory import MemoryStore
from enclave.orchestrator.watcher import WorkspaceWatcher
from enclave.orchestrator.control import ControlServer

log = get_logger("router")

# Minimum interval between Matrix message edits (seconds)
_EDIT_THROTTLE = 5.0

# Max length for accumulated activity messages before starting a new one
_MAX_ACTIVITY_LEN = 3500

# Minimum interval between activity message flushes to Matrix (seconds)
_ACTIVITY_THROTTLE = 3.0

# Volume-based room history purge: trigger when event count exceeds threshold
_ROOM_PURGE_THRESHOLD = 500
_ROOM_PURGE_KEEP = 200


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
        sessions: SessionManager,
        control_room_id: str,
        space_id: str | None = None,
        allowed_users: list[str] | None = None,
        user_mappings: list[UserMapping] | None = None,
        data_dir: str = "",
        priv_broker_socket: str = "",  # Deprecated — ignored
        approval_timeout: float = 300.0,
        idle_timeout: int = 7200,
        memory_config: Any | None = None,
        mimir_config: Any | None = None,
    ):
        self.matrix = matrix
        self.ipc = ipc
        self.sessions = sessions
        # Backward-compat alias — used by ControlServer and a few internal spots
        self.containers = sessions
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

        # Thinking stream state: session_id → {event_id, last_edit, content}
        self._thinking_stream: dict[str, dict[str, Any]] = {}

        # Per-session locks for serializing the initial message creation.
        # IPC messages arrive as concurrent asyncio tasks; without a lock
        # multiple tasks can race to create the initial Matrix message.
        self._stream_locks: dict[str, asyncio.Lock] = {}

        # Timer-based flush tasks for streaming edits.
        # Instead of every IPC task trying to edit, tasks just update the
        # stream dict and a single periodic timer does the actual Matrix edit.
        self._stream_flush_tasks: dict[str, asyncio.Task] = {}  # session_id → Task

        # Activity status message per session (editable tool/thinking display)
        self._activity_msg: dict[str, str] = {}  # session_id → Matrix event_id
        # Accumulated activity lines per session (appended, not replaced)
        self._activity_lines: dict[str, list[str]] = {}  # session_id → list of lines
        # All activity event IDs per session (tracked for purge, no longer redacted)
        self._activity_event_ids: dict[str, list[str]] = {}  # session_id → [event_ids]
        # Activity throttling state
        self._activity_last_flush: dict[str, float] = {}  # session_id → monotonic time
        self._activity_flush_tasks: dict[str, asyncio.Task] = {}  # session_id → pending flush
        self._activity_thread_ids: dict[str, str | None] = {}  # session_id → thread for flush

        # Sub-agent thread tracking
        # session_id → event_id of the message that starts the sub-agent thread
        self._subagent_threads: dict[str, str] = {}

        # Sub-agent parent tracking: sub_session_id → parent_session_id
        self._subagent_parents: dict[str, str] = {}

        # Pending messages queued during session restore (sent once agent is ready)
        self._pending_messages: dict[str, list[dict[str, Any]]] = {}

        # Sessions currently being restored (prevent double-restore)
        self._restoring: set[str] = set()

        # Sessions needing a continuation nudge after nix-shell restart
        self._nix_shell_nudge: set[str] = set()

        # Buffered agent responses waiting for turn_end before posting to Matrix.
        # This prevents flooding Matrix when the agent does multi-turn investigation
        # (each turn produces a response, but only the last one matters).
        self._response_buffer: dict[str, str] = {}  # session_id → content
        self._response_buffer_thread: dict[str, str | None] = {}  # session_id → thread_id
        self._response_flush_tasks: dict[str, asyncio.Task] = {}  # session_id → fallback timer

        # File watchers for workspace change notifications
        self._watchers: dict[str, WorkspaceWatcher] = {}  # session_id → watcher

        # Projects currently being created (prevent double room creation)
        self._creating_projects: set[str] = set()

        # Generic poll awaits: poll_event_id → (asyncio.Event, result list)
        # Used for profile selection polls (and potentially other non-approval polls)
        self._generic_polls: dict[str, tuple[asyncio.Event, list[str]]] = {}

        # ask_user polls: poll_event_id → session_id
        self._ask_user_polls: dict[str, str] = {}

        # Port allocation lock (prevents concurrent allocations of the same port)
        self._port_alloc_lock = asyncio.Lock()

        # Rooms waiting for a user to join before sending queued messages
        # room_id → list of message strings to send once the user joins
        self._awaiting_join: dict[str, list[str]] = {}

        # Turn timing — detect stalled turns
        self._turn_start_time: dict[str, float] = {}

        # Periodic typing indicator refresh (Matrix typing expires after 30s)
        self._typing_tasks: dict[str, asyncio.Task] = {}

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

        # Display manager for desktop interaction
        self._display = DisplayManager()
        self._display.detect_session()

        # ── Idle timeout ──
        self._idle_timeout = idle_timeout  # seconds, 0 = disabled

        # ── Memory stores (per user) ──
        self._memory_stores: dict[str, MemoryStore] = {}  # matrix_user_id → store
        self._memory_config = memory_config
        self._mimir_config = mimir_config
        self._data_dir = data_dir or os.path.expanduser("~/.local/share/enclave")

        # ── Mimir librarian worker ──
        # Drains pending drafts whenever an agent emits a compaction-complete
        # event or a successful mimir_record tool call. None if Mimir is
        # disabled.
        self._mimir_librarian = None
        if mimir_config and getattr(mimir_config, "enabled", False):
            from enclave.orchestrator.mimir_librarian import MimirLibrarianWorker
            agent_name = getattr(mimir_config, "agent_name", "brook") or "brook"
            ws_root = getattr(mimir_config, "workspace_root", "")
            ws_dir = os.path.join(ws_root, agent_name)
            self._mimir_librarian = MimirLibrarianWorker(
                librarian_bin=getattr(
                    mimir_config, "host_librarian_bin",
                    "/usr/local/bin/mimir-librarian",
                ),
                canonical_log=os.path.join(ws_dir, "canonical.log"),
                drafts_dir=os.path.join(ws_dir, "drafts"),
            )

        # ── Audit log ──
        self._audit = AuditLog(self._data_dir)

        # Wire audit into SessionManager
        self.sessions.audit = self._audit

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

        # Start periodic health check
        self._health_task = asyncio.create_task(self._health_check_loop())

        # Start scheduler
        await self._scheduler.start()

        # Start control socket
        await self._control.start()

        # Start Mimir librarian worker (if enabled)
        if self._mimir_librarian is not None:
            self._mimir_librarian.start()

        # Auto-restore sessions that were running before last shutdown
        await self._auto_restore_sessions()

        log.info("Router started")

    async def stop(self) -> None:
        """Clean up — save session state for restore on next start."""
        self.sessions._shutting_down = True
        self.sessions.save_sessions()
        log.info("Session state saved for restore")

        if hasattr(self, "_health_task") and not self._health_task.done():
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass
        await self._scheduler.stop()
        await self._control.stop()
        if self._mimir_librarian is not None:
            await self._mimir_librarian.stop()
        self._perm_db.close()
        log.info("Router stopped")

    async def inject_message(
        self, session_id: str, content: str, attachments: list[dict] | None = None
    ) -> bool:
        """Inject a user message into a session (from control socket).

        Also echoes the message to the agent's Matrix room so users can
        follow the conversation.  Control-socket messages are always
        treated as priority (immediate injection after current tool call).
        """
        session = self.containers.get_session(session_id)
        if not session:
            return False

        resolved_attachments = attachments or []
        # Pre-download attachments if they have URLs but no local_path
        if resolved_attachments and session.workspace_path:
            resolved_attachments = await self._predownload_attachments(
                session, resolved_attachments
            )

        msg = Message(
            type=MessageType.USER_MESSAGE,
            payload={
                "content": content,
                "sender": "control",
                "room_id": session.room_id,
                "thread_id": None,
                "attachments": resolved_attachments,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "priority": True,
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
            return True

        # Agent not connected — try to restore the session
        stopped_session = self.containers.get_any_session_by_room(session.room_id)
        if stopped_session and stopped_session.id not in self._restoring:
            log.info("Control inject: restoring stopped session %s", session_id)
            self._pending_messages.setdefault(session_id, []).append({
                "body": content,
                "sender": "control",
                "room_id": session.room_id,
            })
            await self._restore_session(stopped_session, session.room_id)
            return True
        return False

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
        # Backup every N ticks (interval × ticks = backup period)
        backup_ticks = self.sessions._BACKUP_INTERVAL // self._HEALTH_INTERVAL
        purge_ticks = 300 // self._HEALTH_INTERVAL  # check every ~5 minutes
        tick = 0

        while True:
            try:
                await asyncio.sleep(self._HEALTH_INTERVAL)
                tick += 1

                # Notify systemd watchdog that the event loop is alive
                try:
                    from systemd.daemon import notify
                    notify("WATCHDOG=1")
                except Exception:
                    pass

                crashed = await self.sessions.check_health()
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
                        session = self.sessions.get_session(sid)
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
                self.sessions.save_sessions()

                # Periodic SDK state backups
                if tick % backup_ticks == 0:
                    count = await self.sessions.backup_all_running()
                    if count:
                        log.info("SDK state backed up for %d session(s)", count)

                # Volume-based room history purge (check every ~5 minutes)
                if tick % purge_ticks == 0:
                    await self._check_room_purge()
            except asyncio.CancelledError:
                return
            except Exception as e:
                log.error("Health check error: %s", e)

    def _touch_activity(self, session_id: str) -> None:
        """Record that a session had activity (prevents idle shutdown)."""
        self.sessions.touch_activity(session_id)

    async def _check_idle_sessions(self) -> None:
        """Stop sessions that have been idle beyond the timeout."""
        if self._idle_timeout <= 0:
            return
        now = time.monotonic()
        for session in self.sessions.active_sessions():
            sid = session.id

            # Skip sessions in active turns
            if sid in self._turn_start_time:
                continue

            idle = self.sessions.get_idle_seconds(sid)
            if idle is None:
                # First check — set the timestamp and skip
                self.sessions.touch_activity(sid)
                continue

            if idle < self._idle_timeout:
                continue

            # Check if the container has busy processes
            if await self.sessions.runtime.container_has_processes(sid):
                log.debug("Session %s idle but has running processes", sid)
                continue

            log.info(
                "Session %s idle for %.0fs — shutting down", sid, idle
            )

            # Trigger auto-dreaming before shutdown if enabled
            if self._memory_config and self._memory_config.auto_dreaming:
                await self._trigger_dream_on_shutdown(session)

            await self.matrix.send_message(
                session.room_id,
                "💤 Session idle — shutting down. Send a message to restart.",
            )
            await self.sessions.stop_session(sid, reason="idle")
            self.sessions.clear_activity(sid)

    async def _check_room_purge(self) -> None:
        """Purge room history when event counts exceed the threshold."""
        for session in self.sessions.active_sessions():
            count = self.matrix.get_event_count(session.room_id)
            if count >= _ROOM_PURGE_THRESHOLD:
                log.info(
                    "Room %s has %d events since reset — triggering purge (keep %d)",
                    session.room_id, count, _ROOM_PURGE_KEEP,
                )
                result = await self.matrix.purge_room_history(
                    session.room_id, keep_events=_ROOM_PURGE_KEEP,
                )
                if result >= 0:
                    self.matrix.reset_event_count(session.room_id)
                    if result > 0:
                        await self.matrix.send_message(
                            session.room_id,
                            f"🗑️ Room history trimmed (keeping last ~{_ROOM_PURGE_KEEP} events).",
                        )

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

        # Store context for routing the response back.
        # Don't update if the user replied inside a sub-agent thread —
        # that would permanently redirect all responses into the sub-agent
        # thread even after it completes.
        if thread_id and thread_id not in self._subagent_threads.values():
            self._thread_events[session.id] = thread_id
        # Store the user's event_id so we can ✅ it when the agent responds
        if event_id:
            self._thread_events[f"{session.id}:event_id"] = event_id
            self._thread_events[f"{session.id}:room_id"] = room_id

        # Pre-download attachments so they're available on disk immediately.
        # The agent can read them at any point during the session without
        # needing a blocking IPC round-trip back to the orchestrator.
        resolved_attachments = attachments or []
        if resolved_attachments and session.workspace_path:
            resolved_attachments = await self._predownload_attachments(
                session, resolved_attachments
            )

        # Priority message: strip leading '!' and flag for immediate injection
        priority = False
        if body.startswith("!"):
            body = body[1:].lstrip()
            priority = True

        msg = Message(
            type=MessageType.USER_MESSAGE,
            payload={
                "content": body,
                "sender": sender,
                "room_id": room_id,
                "thread_id": thread_id,
                "attachments": resolved_attachments,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "priority": priority,
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
            self._control.notify_turn_start(session.id)
            self._start_typing_refresh(session)
            # Cancel pending Matrix flush — agent is continuing investigation
            old_task = self._response_flush_tasks.pop(session.id, None)
            if old_task and not old_task.done():
                old_task.cancel()
        elif msg.type == MessageType.TURN_END:
            elapsed = time.monotonic() - self._turn_start_time.pop(session.id, time.monotonic())
            log.info("Agent %s turn ended (%.1fs)", session.id, elapsed)
            self._stop_typing_refresh(session.id)
            self._control.notify_turn_end(session.id)
            # Snapshot SDK state after every interaction
            asyncio.create_task(
                asyncio.to_thread(self.sessions.backup_sdk_state, session)
            )
            # Start a longer debounce for Matrix flush — if a new turn
            # starts soon (multi-turn investigation), it will be cancelled.
            if session.id in self._response_buffer:
                old_task = self._response_flush_tasks.pop(session.id, None)
                if old_task and not old_task.done():
                    old_task.cancel()
                self._response_flush_tasks[session.id] = asyncio.create_task(
                    self._delayed_response_flush(session.id, 30.0)
                )
        elif msg.type == MessageType.STATUS_UPDATE:
            await self._handle_agent_status(session, msg)
        elif msg.type == MessageType.PERMISSION_REQUEST:
            await self._handle_permission_request(session, msg)
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
        elif msg.type == MessageType.NIX_SHELL_REQUEST:
            await self._handle_nix_shell_request(session, msg)
        elif msg.type == MessageType.PORT_REQUEST:
            await self._handle_port_request(session, msg)
        elif msg.type == MessageType.TASK_DONE:
            summary = msg.payload.get("summary", "")
            log.info("Agent %s marked task done: %s", session.id, summary[:80])
            # Discard buffered intermediate response — the task_done summary
            # is the authoritative completion message for Matrix.
            self._response_buffer.pop(session.id, None)
            self._response_buffer_thread.pop(session.id, None)
            old_task = self._response_flush_tasks.pop(session.id, None)
            if old_task and not old_task.done():
                old_task.cancel()
            if summary:
                await self.matrix.send_message(
                    session.room_id,
                    f"✅ {summary}",
                )
        elif msg.type == MessageType.STRUCTURED_RESPONSE:
            await self._handle_structured_response(session, msg)
        elif msg.type == MessageType.ASK_USER:
            question = msg.payload.get("question", "")
            choices = msg.payload.get("choices") or []
            log.info("Agent %s asking user: %s", session.id, question[:80])

            # Discard buffered response — ask_user is the important event
            self._response_buffer.pop(session.id, None)
            self._response_buffer_thread.pop(session.id, None)
            old_task = self._response_flush_tasks.pop(session.id, None)
            if old_task and not old_task.done():
                old_task.cancel()

            # Notify control socket subscribers (web UI)
            if self._control:
                self._control.notify_ask_user(session.id, question, choices)

            thread_id = self._thread_events.get(session.id)
            user_mxid = self._get_user_for_session(session)
            if user_mxid:
                display = user_mxid.split(":")[0].lstrip("@")
                mention_plain = f"@{display}"
                mention_html = (
                    f'<a href="https://matrix.to/#/{user_mxid}">'
                    f"{display}</a>"
                )
            else:
                mention_plain = ""
                mention_html = ""
            if choices:
                answers = [(c, c) for c in choices]
                tagged_q = (
                    f"{mention_plain}: {question}" if mention_plain
                    else question
                )
                poll_eid = await self.matrix.send_poll(
                    session.room_id, tagged_q, answers,
                    thread_event_id=thread_id,
                )
                if poll_eid:
                    self._ask_user_polls[poll_eid] = session.id
            else:
                plain = (
                    f"❓ {mention_plain}: {question}" if mention_plain
                    else f"❓ {question}"
                )
                html = (
                    f"❓ {mention_html}: {question}" if mention_html
                    else None
                )
                await self.matrix.send_message(
                    session.room_id, plain,
                    html_body=html,
                    thread_event_id=thread_id,
                )
        elif msg.type == MessageType.ASK_DEFERRED:
            await self._handle_ask_deferred(session, msg)
        else:
            log.debug(
                "Unhandled IPC message type from %s: %s",
                session_id,
                msg.type.value,
            )

        return None

    def _get_stream_lock(self, session_id: str) -> asyncio.Lock:
        lock = self._stream_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._stream_locks[session_id] = lock
        return lock

    def _flush_key(self, session_id: str, thinking: bool = False) -> str:
        return f"{session_id}:thinking" if thinking else session_id

    def _schedule_stream_flush(
        self, session_id: str, thinking: bool = False
    ) -> None:
        """Schedule a deferred edit for the streaming/thinking message.

        Only one flush timer per stream per session is active at a time.
        The timer fires after _EDIT_THROTTLE seconds, reads the current
        content from the stream dict, and performs a single Matrix edit.
        """
        key = self._flush_key(session_id, thinking)
        if key in self._stream_flush_tasks:
            return  # Timer already scheduled
        task = asyncio.create_task(
            self._do_stream_flush(session_id, thinking)
        )
        self._stream_flush_tasks[key] = task

    def _cancel_stream_flush(
        self, session_id: str, thinking: bool = False
    ) -> None:
        key = self._flush_key(session_id, thinking)
        task = self._stream_flush_tasks.pop(key, None)
        if task and not task.done():
            task.cancel()

    async def _do_stream_flush(
        self, session_id: str, thinking: bool
    ) -> None:
        """Wait for the throttle interval, then edit the Matrix message."""
        try:
            await asyncio.sleep(_EDIT_THROTTLE)
        except asyncio.CancelledError:
            return
        finally:
            key = self._flush_key(session_id, thinking)
            self._stream_flush_tasks.pop(key, None)

        session = self.sessions.get_session(session_id) or self.containers.get_session(session_id)
        if session is None:
            return

        stream = (self._thinking_stream if thinking else self._streaming).get(session_id)
        if not stream or not stream.get("event_id"):
            return

        content = stream.get("content", "")
        if not content:
            return

        now = time.monotonic()
        if thinking:
            preview = content.replace("\n", " ")
            if len(preview) > 800:
                preview = preview[:800] + "…"
            plain = f"🤔 {preview} 🤔"
            html = f"🤔 <i>{_html_escape(preview)}</i> 🤔"
            await self.matrix.edit_message(
                session.room_id, stream["event_id"],
                plain, html_body=html,
            )
        else:
            await self.matrix.edit_message(
                session.room_id, stream["event_id"],
                content + " ▍",
            )
        stream["last_edit"] = now

    async def _handle_agent_delta(
        self, session: Session, msg: Message
    ) -> None:
        """Handle a streaming text delta — WebUI only (no Matrix streaming).

        The final AGENT_RESPONSE will post the complete text to Matrix.
        Streaming deltas are forwarded only to the control socket for the
        WebUI's real-time display.
        """
        content = msg.payload.get("content", "")
        if not content:
            return
        self._control.notify_delta(session.id, content)

    async def _finalize_stale_stream(self, session: Session) -> None:
        """Finalize a stream belonging to a previous turn: strip the cursor
        so the old message stops looking in-progress, then clear tracking so
        the next delta creates a new message."""
        self._cancel_stream_flush(session.id)
        stream = self._streaming.pop(session.id, None)
        if not stream or not stream.get("event_id"):
            return
        final = stream.get("content", "")
        if final:
            try:
                await self.matrix.edit_message(
                    session.room_id, stream["event_id"], final,
                )
            except Exception as exc:
                log.warning("Failed to finalize stale stream: %s", exc)

    async def _handle_agent_thinking(
        self, session: Session, msg: Message
    ) -> None:
        """Handle an intent/thinking/reasoning update — WebUI only.

        All thinking/reasoning events go to the control socket (WebUI) but
        are suppressed from Matrix to reduce notification volume.
        """
        # Skip activity in sub-agent threads (high volume, rarely useful to user)
        if session.id in self._subagent_threads:
            return

        # Short intent (e.g. "Exploring the codebase")
        intent = msg.payload.get("intent", "")
        if intent:
            log.debug("Agent %s intent: %s", session.id, intent)
            self._control.notify_activity(session.id, f"💭 {intent}")
            return

        # Accumulated thinking content (streaming)
        thinking = msg.payload.get("thinking_content", "")
        if thinking:
            self._control.notify_thinking(session.id, thinking, "delta")
            return

        # Full reasoning text (complete thinking block)
        reasoning = msg.payload.get("reasoning", "")
        if reasoning:
            self._control.notify_thinking(session.id, reasoning, "end")
            return

        # Streaming reasoning delta
        delta = msg.payload.get("reasoning_delta", "")
        if delta:
            preview = delta.strip()[:150].replace("\n", " ")
            if preview:
                self._control.notify_thinking(session.id, preview, "delta")

    # Friendly display names for common SDK tools
    _TOOL_LABELS: dict[str, str] = {
        "bash": "🖥️",
        "read_bash": "📖",
        "write_bash": "⌨️",
        "stop_bash": "⏹️",
        "view": "📄",
        "edit": "✏️",
        "create": "📝",
        "grep": "🔍",
        "glob": "📁",
        "web_fetch": "🌐",
        "web_search": "🔎",
        "task": "🤖",
        "read_agent": "📨",
        "sql": "🗃️",
        "ask_user": "❓",
        "list_bash": "📋",
    }

    async def _handle_tool_start(
        self, session: Session, msg: Message
    ) -> None:
        """Handle tool execution start — show tool name and detail."""
        tool_name = msg.payload.get("tool_name", "unknown")
        description = msg.payload.get("description", "")
        detail = msg.payload.get("detail", "")
        # Skip internal/noisy tools
        if tool_name in ("report_intent",):
            return
        log.debug("Agent %s tool start: %s", session.id, tool_name)
        self._control.notify_tool_start(session.id, tool_name, detail or description or "")
        self._audit.log(
            "tool_start", session_id=session.id,
            tool=tool_name, description=description,
        )
        # Skip activity in sub-agent threads (high volume, rarely useful to user)
        if session.id in self._subagent_threads:
            return
        thread_id = self._thread_events.get(session.id)

        icon = self._TOOL_LABELS.get(tool_name, "🔧")
        # Build a concise, informative line
        if detail:
            plain = f"{icon} {detail}"
            html = f"{icon} <code>{_html_escape(detail)}</code>"
        elif description:
            plain = f"{icon} {description}"
            html = f"{icon} {_html_escape(description)}"
        else:
            plain = f"{icon} {tool_name}"
            html = f"{icon} <code>{_html_escape(tool_name)}</code>"
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
        self._control.notify_tool_complete(session.id, tool_name, success)
        self._audit.log(
            "tool_complete", session_id=session.id,
            tool=tool_name, success=success,
        )
        # Mimir librarian: drain pending drafts after a successful
        # mimir_record call (the agent just queued a new draft).
        if (
            success
            and tool_name == "mimir_record"
            and self._mimir_librarian is not None
        ):
            self._mimir_librarian.trigger(
                f"mimir_record by {session.id}"
            )
        # No Matrix activity messages anymore — typing indicator is managed
        # only at turn_start (via refresh loop) and cleared on agent_response.

    # ── Typing indicator refresh ──

    def _start_typing_refresh(self, session: Session) -> None:
        """Start periodic typing indicator refresh for a session's turn."""
        self._stop_typing_refresh(session.id)

        async def _refresh_loop() -> None:
            try:
                while True:
                    await asyncio.sleep(20)
                    await self.matrix.set_typing(session.room_id, True)
            except asyncio.CancelledError:
                pass

        self._typing_tasks[session.id] = asyncio.create_task(_refresh_loop())

    def _stop_typing_refresh(self, session_id: str) -> None:
        """Cancel the typing refresh task for a session."""
        task = self._typing_tasks.pop(session_id, None)
        if task and not task.done():
            task.cancel()

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
        # Clear the sub-agent thread so further events go to the main thread.
        # Also clear _thread_events if a user reply inside the sub-agent thread
        # polluted it — otherwise all future responses stay trapped in the thread.
        sub_thread = self._subagent_threads.pop(session.id, None)
        if sub_thread and self._thread_events.get(session.id) == sub_thread:
            self._thread_events.pop(session.id, None)
        # Cancel any pending flush and detach activity tracking
        task = self._activity_flush_tasks.pop(session.id, None)
        if task and not task.done():
            task.cancel()
        self._activity_msg.pop(session.id, None)
        self._activity_lines.pop(session.id, None)
        self._activity_event_ids.pop(session.id, None)
        self._activity_last_flush.pop(session.id, None)
        self._activity_thread_ids.pop(session.id, None)

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
        """Buffer an activity line — WebUI-only (suppressed from Matrix).

        Minor events (tool calls, thinking) are sent only to the control
        socket (WebUI) to avoid flooding Matrix and draining phone battery.
        The control socket notify_* calls happen at the call sites before
        this function is invoked.
        """
        # Suppressed from Matrix — minor events go to WebUI only via
        # control socket notify_* calls at the call sites.
        return

    async def _delayed_activity_flush(
        self, session_id: str, delay: float
    ) -> None:
        """Sleep then flush buffered activity lines."""
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        self._activity_flush_tasks.pop(session_id, None)
        session = self.sessions.get_session(session_id)
        if session and session_id in self._activity_lines:
            await self._flush_activity(session)

    async def _flush_activity(self, session: Session) -> None:
        """Send or edit the accumulated activity lines to Matrix."""
        lines = self._activity_lines.get(session.id, [])
        if not lines:
            return

        # Cancel any pending delayed flush (we're flushing now)
        task = self._activity_flush_tasks.pop(session.id, None)
        if task and not task.done():
            task.cancel()

        thread_id = self._activity_thread_ids.get(session.id)
        combined_plain = "\n".join(t for t, _ in lines)
        combined_html = "<br/>".join(h for _, h in lines)
        existing = self._activity_msg.get(session.id)

        if existing and len(combined_plain) <= _MAX_ACTIVITY_LEN:
            await self.matrix.edit_message(
                session.room_id, existing, combined_plain,
                html_body=combined_html,
            )
        elif existing:
            # Over the limit — finalise current message and start a new one
            self._activity_msg.pop(session.id, None)
            last_text, last_html = lines[-1]
            self._activity_lines[session.id] = [(last_text, last_html)]
            event_id = await self.matrix.send_message(
                session.room_id, last_text, html_body=last_html,
                thread_event_id=thread_id,
            )
            if event_id:
                self._activity_msg[session.id] = event_id
                self._activity_event_ids.setdefault(session.id, []).append(event_id)
        else:
            event_id = await self.matrix.send_message(
                session.room_id, combined_plain, html_body=combined_html,
                thread_event_id=thread_id,
            )
            if event_id:
                self._activity_msg[session.id] = event_id
                self._activity_event_ids.setdefault(session.id, []).append(event_id)

        self._activity_last_flush[session.id] = time.monotonic()
        await self.matrix.set_typing(session.room_id, True)

    async def _handle_agent_response(
        self, session: Session, msg: Message
    ) -> None:
        """Buffer the agent response — flush to Matrix on turn_end.

        The WebUI gets the response immediately via control socket.
        Matrix delivery is deferred so that multi-turn investigation
        only sends the last response, not every intermediate thought.
        """
        content = msg.payload.get("content", "")
        if not content:
            return

        # Notify control socket subscribers (WebUI) immediately
        self._control.notify_response(session.id, content)

        thread_id = self._thread_events.get(session.id)
        log.info(
            "Buffered agent response for %s (thread=%s): %s",
            session.room_id, thread_id, content[:80],
        )

        # Buffer for Matrix — replaced if another response arrives before turn_end
        self._response_buffer[session.id] = content
        self._response_buffer_thread[session.id] = thread_id

        # Cancel any existing flush timer and start a long fallback.
        # The actual flush is managed by TURN_END (30s debounce) and
        # TASK_DONE (immediate).  This is a safety net in case those
        # events never arrive.
        old_task = self._response_flush_tasks.pop(session.id, None)
        if old_task and not old_task.done():
            old_task.cancel()
        self._response_flush_tasks[session.id] = asyncio.create_task(
            self._delayed_response_flush(session.id, 120.0)
        )

    async def _delayed_response_flush(self, session_id: str, delay: float) -> None:
        """Fallback: flush buffered response to Matrix if turn_end doesn't arrive."""
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        self._response_flush_tasks.pop(session_id, None)
        await self._flush_response_to_matrix(session_id)

    async def _flush_response_to_matrix(self, session_id: str) -> None:
        """Send the buffered response to Matrix and do emoji cleanup."""
        content = self._response_buffer.pop(session_id, None)
        thread_id = self._response_buffer_thread.pop(session_id, None)
        if not content:
            return

        # Cancel fallback timer if we're being called from turn_end
        old_task = self._response_flush_tasks.pop(session_id, None)
        if old_task and not old_task.done():
            old_task.cancel()

        session = self.sessions.get_session(session_id)
        if not session:
            return

        log.info(
            "Forwarding agent response to %s (thread=%s): %s",
            session.room_id, thread_id, content[:80],
        )

        # Stop typing
        self._stop_typing_refresh(session_id)
        await self.matrix.set_typing(session.room_id, False)

        # Send to Matrix
        await self.matrix.send_message(
            session.room_id,
            content,
            thread_event_id=thread_id,
        )

        # Complete the emoji flow: remove 🤔, add ✅
        user_event_id = self._thread_events.pop(
            f"{session_id}:event_id", None
        )
        user_room_id = self._thread_events.pop(
            f"{session_id}:room_id", None
        )
        if user_event_id and user_room_id:
            thinking_key = f"{user_room_id}:{user_event_id}"
            thinking_eid = self._pending_reactions.pop(thinking_key, None)
            if thinking_eid:
                await self.matrix.redact_event(user_room_id, thinking_eid)
            await self.matrix.send_reaction(
                user_room_id, user_event_id, "✅"
            )

    async def _handle_structured_response(
        self, session: Session, msg: Message
    ) -> None:
        """Handle a structured response — rich card to WebUI, summary to Matrix."""
        payload = msg.payload
        summary = payload.get("summary", "")
        title = payload.get("title", "")
        if not summary:
            return

        log.info(
            "Structured response from %s: %s — %s",
            session.id, title or "(no title)", summary[:80],
        )

        # Notify control socket (WebUI gets full structured payload)
        if self._control:
            self._control.notify_structured_response(session.id, payload)

        # Build plain-text for Matrix: **title**\n\nsummary\n\nactions
        parts: list[str] = []
        if title:
            parts.append(f"**{title}**")
        parts.append(summary)
        actions = payload.get("actions") or []
        if actions:
            action_lines = []
            for i, a in enumerate(actions, 1):
                label = a.get("label", f"Option {i}")
                action_lines.append(f"{i}️⃣ {label}")
            parts.append("\n".join(action_lines))
        matrix_text = "\n\n".join(parts)

        # Buffer for Matrix like regular responses (debounce)
        thread_id = self._thread_events.get(session.id)
        self._response_buffer[session.id] = matrix_text
        self._response_buffer_thread[session.id] = thread_id
        old_task = self._response_flush_tasks.pop(session.id, None)
        if old_task and not old_task.done():
            old_task.cancel()
        self._response_flush_tasks[session.id] = asyncio.create_task(
            self._delayed_response_flush(session.id, 120.0)
        )

    async def _handle_ask_deferred(
        self, session: Session, msg: Message
    ) -> None:
        """Handle a non-blocking question from the agent."""
        from enclave.webui.deferred_asks import get_deferred_asks_store

        payload = msg.payload
        question = payload.get("question", "")
        if not question:
            return

        log.info("Agent %s deferred ask: %s", session.id, question[:80])

        # Derive workspace_base from session's workspace_path (its parent)
        workspace_base = Path(session.workspace_path).parent
        store = get_deferred_asks_store(workspace_base)
        ask = store.add(
            session_id=session.id,
            question=question,
            choices=payload.get("choices"),
            context=payload.get("context"),
            priority=payload.get("priority", "normal"),
            tags=payload.get("tags"),
        )

        # Notify control socket so WebUI can update badge count
        if self._control:
            self._control.notify_deferred_ask(session.id, ask)

    async def _handle_agent_status(
        self, session: Session, msg: Message
    ) -> None:
        """Handle agent status updates."""
        status = msg.payload.get("status", "unknown")
        copilot = msg.payload.get("copilot_available", False)

        if status == "doom_loop_detected":
            turns = msg.payload.get("turns", 0)
            elapsed = msg.payload.get("elapsed_seconds", 0)
            nudge_count = msg.payload.get("nudge_count", 1)
            signals = msg.payload.get("signals", []) or []
            signal_lines = "\n".join(f"• {s}" for s in signals)
            notice = (
                f"🔄 **Doom-loop nudge #{nudge_count}** "
                f"({turns} turns, {elapsed // 60}min)\n{signal_lines}"
            )
            log.info(
                "Doom loop nudge for %s: turns=%d elapsed=%ds signals=%s",
                session.id, turns, elapsed, signals,
            )
            try:
                await self.matrix.send_message(session.room_id, notice)
            except Exception as e:
                log.warning("Failed to post doom-loop notice to Matrix: %s", e)
            return

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

            # Send continuation nudge after nix-shell restart
            if session.id in self._nix_shell_nudge:
                self._nix_shell_nudge.discard(session.id)
                nix_path = session.nix_shell_path or "unknown"
                nudge = Message(
                    type=MessageType.USER_MESSAGE,
                    payload={
                        "content": (
                            "[Enclave Coordinator] You have been restarted under "
                            f"`nix-shell {nix_path}`. Your environment is now "
                            "active. Please continue with your current task."
                        ),
                        "sender": "system",
                        "room_id": session.room_id,
                    },
                )
                await self.ipc.send_to(session.id, nudge)

            # Flush any messages queued during session restore
            pending = self._pending_messages.pop(session.id, [])
            for queued in pending:
                log.info(
                    "Sending queued message to %s: %s",
                    session.id, queued["body"][:60],
                )
                # Pre-download any attachments before sending
                raw_atts = queued.get("attachments") or []
                if raw_atts and session.workspace_path:
                    raw_atts = await self._predownload_attachments(
                        session, raw_atts
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
                        "attachments": raw_atts,
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
            msgs = msg.payload.get("messages_removed")
            tokens = msg.payload.get("tokens_removed")
            pre = msg.payload.get("pre_compaction_tokens")
            post = msg.payload.get("post_compaction_tokens")
            log.info(
                "Agent %s: compaction complete (%s msgs, %s tokens removed, %s → %s)",
                session.id, msgs, tokens, pre, post,
            )
            thread_id = self._thread_events.get(session.id)
            parts = []
            if pre is not None and post is not None:
                try:
                    parts.append(f"{int(pre):,} → {int(post):,} tokens")
                except (ValueError, TypeError):
                    pass
            if msgs is not None:
                try:
                    parts.append(f"{int(msgs)} messages removed")
                except (ValueError, TypeError):
                    pass
            elif tokens is not None:
                try:
                    parts.append(f"{int(tokens):,} tokens freed")
                except (ValueError, TypeError):
                    pass
            detail = ", ".join(parts) if parts else "context compacted"
            await self._update_activity(
                session, f"🗜️ Compacted: {detail}", thread_id,
            )
            # Mimir librarian: compaction always submits a snapshot draft
            # (see SESSION_COMPACTION_START hook in the agent), so drain
            # pending drafts shortly after.
            if self._mimir_librarian is not None:
                self._mimir_librarian.trigger(
                    f"compaction_complete on {session.id}"
                )

        elif status == "nix_shell_active":
            nix_path = msg.payload.get("path", "?")
            log.info("Agent %s: running under nix-shell %s", session.id, nix_path)
            await self.matrix.send_message(
                session.room_id,
                f"✅ Nix environment active: `{nix_path}`",
            )

        elif status == "nix_shell_failed":
            nix_path = msg.payload.get("path", "?")
            log_path = msg.payload.get("log", "")
            log_content = msg.payload.get("log_content", "")
            log.warning(
                "Agent %s: nix-shell failed for %s", session.id, nix_path,
            )
            fail_msg = f"⚠️ Nix-shell failed for `{nix_path}` — running without it."
            if log_path:
                fail_msg += f"\nLog: `{log_path}`"
            if log_content:
                # Truncate to avoid huge messages
                preview = log_content.strip()[-500:]
                fail_msg += f"\n```\n{preview}\n```"
            await self.matrix.send_message(session.room_id, fail_msg)
            # Also inform the SDK session so the agent LLM knows
            await self.ipc.send_to(session.id, Message(
                type=MessageType.USER_MESSAGE,
                payload={
                    "content": (
                        f"[System] The nix-shell switch to `{nix_path}` failed. "
                        f"You are running without the nix environment. "
                        f"Check `{log_path}` for details. "
                        "You can fix the nix expression and try `enter_nix_shell` again."
                    ),
                    "sender": "system",
                },
            ))

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

        # Notify WebUI so images show in the web interface
        if event_id:
            import mimetypes
            mimetype = mimetypes.guess_type(file_path)[0] or ""
            filename = os.path.basename(file_path)
            self._control.notify_file_send(
                session.id, filename=filename, mimetype=mimetype,
                event_id=event_id, file_path=file_path,
            )

        return Message(
            type=MessageType.AGENT_RESPONSE,
            payload={
                "sent": event_id is not None,
                "event_id": event_id,
            },
            reply_to=msg.id,
        )

    async def _predownload_attachments(
        self, session: Session, attachments: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Pre-download attachments to the session workspace.

        Downloads each attachment eagerly so the agent can access them
        directly from disk without a blocking IPC round-trip.  Returns
        a new attachment list with ``local_path`` set (container-relative)
        for each successfully downloaded file.
        """
        attach_dir = Path(session.workspace_path) / ".attachments"
        attach_dir.mkdir(parents=True, exist_ok=True)

        resolved: list[dict[str, Any]] = []
        for att in attachments:
            url = att.get("url", "")
            filename = att.get("filename", "attachment")
            encryption = att.get("encryption")

            if not url or not url.startswith("mxc://"):
                resolved.append(att)
                continue

            # Use a unique name so concurrent/sequential uploads don't collide
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            stem = Path(filename).stem
            suffix = Path(filename).suffix or ".bin"
            unique_name = f"{stem}-{ts}{suffix}"
            host_path = attach_dir / unique_name

            try:
                success = await self.matrix.download_media(
                    url, str(host_path), encryption=encryption,
                )
            except Exception as e:
                log.error("Pre-download failed for %s: %s", filename, e)
                resolved.append(att)
                continue

            if success:
                # Container sees /workspace/.attachments/<unique_name>
                container_path = f"/workspace/.attachments/{unique_name}"
                resolved.append({
                    **att,
                    "local_path": container_path,
                    "host_path": str(host_path),
                })
                log.info(
                    "Pre-downloaded %s → %s (%s)",
                    filename, host_path, att.get("content_type", ""),
                )
            else:
                log.warning("Pre-download failed for %s (mxc download returned false)", filename)
                resolved.append(att)

        return resolved

    async def _handle_download_request(
        self, session: Session, msg: Message
    ) -> Message | None:
        """Handle a file download request from an agent."""
        url = msg.payload.get("url", "")
        dest = msg.payload.get("dest", "")
        encryption = msg.payload.get("encryption")

        if not url or not dest:
            return Message(
                type=MessageType.AGENT_RESPONSE,
                payload={"error": "url and dest are required"},
                reply_to=msg.id,
            )

        # Translate container path → host path
        # Agent sees /workspace/... but host stores at session.workspace_path/...
        if dest.startswith("/workspace/") and session.workspace_path:
            dest = os.path.join(
                session.workspace_path, dest[len("/workspace/"):]
            )

        if url.startswith("mxc://"):
            success = await self.matrix.download_media(
                url, dest, encryption=encryption,
            )
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

    async def _handle_nix_shell_request(
        self, session: Session, msg: Message,
    ) -> None:
        """Handle an agent's request to restart under a nix-shell environment.

        The agent sends a container-relative path (e.g. /workspace/shell.nix).
        We verify the file exists on the host, store the config, then restart
        the session.  The entrypoint will wrap execution in ``nix-shell``.
        """
        container_path = msg.payload.get("path", "")
        if not container_path:
            log.warning("nix_shell_request from %s: missing path", session.id)
            return

        # Map container path → host path for validation
        host_path = container_path
        if container_path.startswith("/workspace/") and session.workspace_path:
            host_path = os.path.join(
                session.workspace_path,
                container_path[len("/workspace/"):],
            )

        if not os.path.isfile(host_path):
            log.warning(
                "nix_shell_request from %s: file not found: %s (host: %s)",
                session.id, container_path, host_path,
            )
            # Inform the agent via Matrix (agent is about to die anyway on restart,
            # but if the file doesn't exist we skip the restart entirely)
            await self.matrix.send_message(
                session.room_id,
                f"⚠️ Nix shell file not found: `{container_path}`",
            )
            return

        log.info(
            "nix_shell_request from %s: %s → restarting under nix-shell",
            session.id, container_path,
        )

        # Persist the nix shell path (container-relative, used by entrypoint)
        session.nix_shell_path = container_path
        self.sessions.save_sessions()

        # Notify via Matrix so the user sees what's happening
        await self.matrix.send_message(
            session.room_id,
            f"🔄 Restarting under `nix-shell {container_path}` …",
        )

        # Stop → start cycle (SDK resumes from checkpoint)
        await self.sessions.stop_session(session.id, reason="nix_shell_switch")

        # Create new IPC socket for the restarted container
        if self.ipc:
            socket_path = await self.ipc.create_socket(session.id)
            session.socket_path = str(socket_path)

        ok, error = await self.sessions.start_session(session.id)
        if not ok:
            log.error(
                "Failed to restart %s under nix-shell: %s",
                session.id, error,
            )
            await self.matrix.send_message(
                session.room_id,
                f"❌ Restart failed: {error}",
            )
        else:
            # Mark for continuation nudge when the agent reports ready
            self._nix_shell_nudge.add(session.id)

    async def _handle_port_request(
        self, session: Session, msg: Message,
    ) -> None:
        """Handle an agent's request to map a container port to a host port.

        The allocation is idempotent and permanent — persisted across restarts.
        The port only becomes active after the next container restart.
        """
        container_port = msg.payload.get("container_port")
        protocol = msg.payload.get("protocol", "tcp").lower()

        # Validate inputs
        if not container_port or not isinstance(container_port, int):
            await self._reply_port_request(session, msg, error="container_port must be an integer")
            return
        if container_port < 1 or container_port > 65535:
            await self._reply_port_request(session, msg, error="container_port must be 1-65535")
            return
        if protocol not in ("tcp", "udp"):
            await self._reply_port_request(session, msg, error="protocol must be 'tcp' or 'udp'")
            return

        # Reject host-mode sessions (no container namespace to publish)
        profile = self.sessions.config.get_profile(session.profile)
        if profile.image == "":
            await self._reply_port_request(
                session, msg,
                error="Port mapping is not available for host-mode sessions (no container).",
            )
            return

        async with self._port_alloc_lock:
            # Idempotent: check if this mapping already exists
            for pm in session.port_mappings:
                if pm["container_port"] == container_port and pm.get("protocol", "tcp") == protocol:
                    host_port = pm["host_port"]
                    hostname = self.sessions.config.get_public_hostname()
                    is_active = session.status == "running"
                    await self._reply_port_request(
                        session, msg,
                        host_port=host_port, hostname=hostname,
                        active=is_active, restart_required=False,
                        already_existed=True,
                    )
                    return

            # Allocate a free host port from the configured range
            used_ports = set()
            for s in self.sessions.list_sessions():
                for pm in s.port_mappings:
                    used_ports.add(pm["host_port"])

            host_port = None
            range_start = self.sessions.config.port_range_start
            range_end = self.sessions.config.port_range_end
            bind_addr = self.sessions.config.port_bind_address

            for candidate in range(range_start, range_end + 1):
                if candidate in used_ports:
                    continue
                # Check if port is actually available on the host
                import socket as _socket
                try:
                    sock_type = _socket.SOCK_STREAM if protocol == "tcp" else _socket.SOCK_DGRAM
                    with _socket.socket(_socket.AF_INET, sock_type) as s:
                        s.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
                        s.bind((bind_addr, candidate))
                    host_port = candidate
                    break
                except OSError:
                    continue

            if host_port is None:
                await self._reply_port_request(
                    session, msg,
                    error=f"No free ports in range {range_start}-{range_end}",
                )
                return

            # Persist the mapping
            session.port_mappings.append({
                "container_port": container_port,
                "host_port": host_port,
                "protocol": protocol,
            })
            self.sessions.save_sessions()

        hostname = self.sessions.config.get_public_hostname()
        log.info(
            "Port mapped for %s: %s:%d → container:%d/%s",
            session.id, hostname, host_port, container_port, protocol,
        )

        # Notify Matrix
        await self.matrix.send_message(
            session.room_id,
            f"🔌 Port mapped: `{hostname}:{host_port}` → container `{container_port}/{protocol}`\n"
            f"⚠️ Restart required to activate.",
        )

        await self._reply_port_request(
            session, msg,
            host_port=host_port, hostname=hostname,
            active=False, restart_required=True,
        )

    async def _reply_port_request(
        self, session: Session, msg: Message, *,
        host_port: int | None = None,
        hostname: str = "",
        active: bool = False,
        restart_required: bool = False,
        already_existed: bool = False,
        error: str = "",
    ) -> None:
        """Send a reply to a PORT_REQUEST message."""
        payload: dict[str, Any] = {}
        if error:
            payload["error"] = error
        else:
            payload.update({
                "host_port": host_port,
                "hostname": hostname,
                "active": active,
                "restart_required": restart_required,
                "already_existed": already_existed,
            })
        reply = Message(
            type=MessageType.PORT_REQUEST,
            payload=payload,
            reply_to=msg.id,
        )
        await self.ipc.send_to(session.id, reply)

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

    async def _handle_mount_request(
        self, session: Session, msg: Message
    ) -> None:
        """Handle a dynamic mount request from an agent.

        Flow: post approval poll → if approved, add mount to session config
        → restart container so podman picks up the new -v flag → agent
        resumes from SDK checkpoint with mount available.
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

        # Validate source path exists on host and user has access
        if not os.path.exists(source_path):
            await self.ipc.send_to(session.id, Message(
                type=MessageType.MOUNT_RESPONSE,
                payload={
                    "approved": False,
                    "source_path": source_path,
                    "error": f"Path does not exist on host: {source_path}",
                },
                reply_to=msg.id,
            ))
            return

        if not os.access(source_path, os.R_OK):
            await self.ipc.send_to(session.id, Message(
                type=MessageType.MOUNT_RESPONSE,
                payload={
                    "approved": False,
                    "source_path": source_path,
                    "error": f"No read access to: {source_path}",
                },
                reply_to=msg.id,
            ))
            return

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
            await self.ipc.send_to(session.id, Message(
                type=MessageType.MOUNT_RESPONSE,
                payload={
                    "approved": False,
                    "source_path": source_path,
                    "error": f"Request {status.value}",
                },
                reply_to=msg.id,
            ))
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

        # Create mount name from source path
        mount_name = (
            source_path.strip("/")
            .replace("/", "-")
            .replace(" ", "-")
            .replace("..", "")[:64]
        )
        container_path = f"/workspace/{mount_name}"

        # Add mount to session config (persisted across restarts)
        mount_entry = {"source": source_path, "mount_name": mount_name}
        if mount_entry not in session.extra_mounts:
            session.extra_mounts.append(mount_entry)
            self.sessions.save_sessions()

        # Notify agent — the session will restart to apply the mount
        await self.ipc.send_to(session.id, Message(
            type=MessageType.MOUNT_RESPONSE,
            payload={
                "approved": True,
                "source_path": source_path,
                "container_path": container_path,
                "mount_name": mount_name,
                "restarting": True,
            },
            reply_to=msg.id,
        ))

        await self.matrix.send_message(
            session.room_id,
            f"📂 Mount approved: `{source_path}` → `{container_path}`\n"
            f"🔄 Restarting container to apply mount...",
        )

        # Restart the container — agent will resume from SDK checkpoint
        log.info("Restarting session %s to apply mount: %s", session.id, source_path)
        await self.sessions.stop_session(session.id, reason="mount_added")
        socket_path = await self.ipc.create_socket(session.id)
        session.socket_path = str(socket_path)
        started, error = await self.sessions.start_session(session.id)
        if started:
            await self.matrix.send_message(
                session.room_id,
                f"✅ Container restarted with `{container_path}` mounted.",
            )
        else:
            await self.matrix.send_message(
                session.room_id,
                f"❌ Failed to restart container: {error}",
            )

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

        # ask_user polls — forward the selected answer to the agent
        ask_session_id = self._ask_user_polls.pop(poll_event_id, None)
        if ask_session_id is not None:
            answer_text = answer_ids[0] if answer_ids else "(no selection)"
            log.info(
                "Poll answer from %s for %s: %s",
                sender, ask_session_id, answer_text,
            )
            await self.ipc.send_to(ask_session_id, Message(
                type=MessageType.USER_MESSAGE,
                payload={
                    "content": answer_text,
                    "sender": sender,
                    "room_id": room_id,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            ))
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
        # Cancel pending stream flush timers
        self._cancel_stream_flush(session_id)
        self._cancel_stream_flush(session_id, thinking=True)
        # Clean up streaming state
        self._streaming.pop(session_id, None)
        self._thinking_stream.pop(session_id, None)
        self._stream_locks.pop(session_id, None)
        # Cancel pending activity flush and clean up tracking
        task = self._activity_flush_tasks.pop(session_id, None)
        if task and not task.done():
            task.cancel()
        self._activity_msg.pop(session_id, None)
        self._activity_lines.pop(session_id, None)
        self._activity_event_ids.pop(session_id, None)
        self._activity_last_flush.pop(session_id, None)
        self._activity_thread_ids.pop(session_id, None)

        # Stop file watcher
        await self._stop_watcher(session_id)

        # Delegate disconnect handling (Matrix notifications, audit) to SessionManager
        await self.sessions.on_agent_disconnect(session_id)

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

        # Write key memories to workspace for agent to read.
        # When Mimir is enabled, skip this — those memories should already
        # be in the Mimir corpus (via bulk import) and recall is the agent's
        # responsibility, not pre-loaded context.
        mimir_active = bool(
            self._mimir_config and getattr(self._mimir_config, "enabled", False)
        )
        if sender and not mimir_active:
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

        # Require approval for GUI launches
        status, scope, pattern = await self._approval.request_permission(
            session_id=session.id,
            session_name=session.name,
            project_name=session.name,
            perm_type=PermissionType.FILESYSTEM,
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
        """Handle the kill command — stop and delete a session."""
        if not cmd.has_args:
            await self._reply_control(
                "Usage: `kill <session-id>` — stops and deletes a session."
            )
            return

        session_id = cmd.args[0]
        removed = await self.sessions.delete_session(session_id, reason="kill")
        self._audit.log("session_killed", session_id=session_id)

        if removed:
            await self._reply_control(
                f"✅ Session `{session_id}` deleted."
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
        try:
            await self.sessions.delete_session(session.id, reason="cleanup")
            return True
        except Exception as e:
            log.error("Failed to clean up session %s: %s", session.id, e)
            return False
