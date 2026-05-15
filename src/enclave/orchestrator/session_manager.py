"""Session manager — single owner of session lifecycle.

Coordinates all subsystems: runtime (containers/host processes), Matrix rooms,
IPC sockets, file watchers, workspace directories, and audit logging.

All session lifecycle operations (create, start, stop, delete, restore) go
through this class.  Callers should never coordinate subsystems directly.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from enclave.common.config import ContainerConfig, MimirConfig
from enclave.common.logging import get_logger

if TYPE_CHECKING:
    from enclave.orchestrator.control import ControlServer
    from enclave.orchestrator.ipc import IPCServer
    from enclave.orchestrator.matrix_client import EnclaveMatrixClient

log = get_logger("sessions")


# ── Session data ──────────────────────────────────────────────────────


@dataclass
class Session:
    """Represents an agent session (container or host-mode)."""

    id: str
    name: str
    room_id: str
    container_id: str | None = None
    workspace_path: str = ""
    socket_path: str = ""
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    status: str = "created"  # created, starting, running, stopping, stopped
    profile: str = ""  # container profile name (e.g., "dev", "light")
    image: str = ""  # resolved container image for this session
    user_display_name: str = ""
    user_pronouns: str = ""
    host_pid: int | None = None  # PID for host-mode subprocess agents
    nix_shell_path: str = ""  # path to shell.nix/flake.nix for nix-shell wrapping
    pending_nix_nudge: bool = False  # needs continuation nudge after nix-shell restart
    extra_mounts: list[dict[str, str]] = field(default_factory=list)  # [{source, mount_name}]
    port_mappings: list[dict[str, Any]] = field(default_factory=list)  # [{container_port, host_port, protocol}]
    # ACP remote agent fields
    acp_host: str = ""  # Remote ACP server hostname/IP
    acp_port: int = 0  # Remote ACP server port
    acp_session_id: str = ""  # The ACP session ID on the remote CLI
    acp_remote_cwd: str = ""  # Working directory on the remote machine


def _slugify(name: str) -> str:
    """Convert a name to a filesystem/container-safe slug."""
    return (
        name.lower()
        .replace(" ", "-")
        .replace("_", "-")
        .replace(".", "-")
        .strip("-")[:32]
    )


# ── Session Manager ──────────────────────────────────────────────────


class SessionManager:
    """Owns every session and orchestrates all lifecycle operations.

    Subsystems are injected at construction and used transparently by
    the lifecycle methods so callers never have to coordinate them.
    """

    def __init__(
        self,
        config: ContainerConfig,
        *,
        matrix: EnclaveMatrixClient | None = None,
        ipc: IPCServer | None = None,
        audit: Any | None = None,
        mimir: MimirConfig | None = None,
    ):
        self.config = config
        self.matrix = matrix
        self.ipc = ipc
        self.audit = audit
        self.mimir = mimir

        # The runtime manager handles the low-level podman / host-process
        # operations.  Imported lazily to avoid circular deps.
        from enclave.orchestrator.container import ContainerManager
        self.runtime = ContainerManager(config, mimir=mimir)

        # Session storage — runtime manager no longer owns this.
        self._sessions: dict[str, Session] = {}
        self._sessions_file = Path(config.session_base) / "sessions.json"

        # Ensure directories exist
        Path(config.workspace_base).mkdir(parents=True, exist_ok=True)
        Path(config.session_base).mkdir(parents=True, exist_ok=True)

        # Activity tracking (moved from Router)
        self._last_activity: dict[str, float] = {}  # session_id → monotonic

        self._shutting_down = False

        self._load_sessions()

    # ── Persistence ───────────────────────────────────────────────────

    def _load_sessions(self) -> None:
        """Load sessions from disk."""
        if not self._sessions_file.exists():
            return
        try:
            data = json.loads(self._sessions_file.read_text())
            for s in data:
                saved_status = s.get("status", "stopped")
                session = Session(
                    id=s["id"],
                    name=s["name"],
                    room_id=s["room_id"],
                    workspace_path=s.get("workspace_path", ""),
                    socket_path=s.get("socket_path", ""),
                    created_at=s.get("created_at", ""),
                    status="was_running" if saved_status == "running" else "stopped",
                    profile=s.get("profile", ""),
                    image=s.get("image", ""),
                    user_display_name=s.get("user_display_name", ""),
                    user_pronouns=s.get("user_pronouns", ""),
                    nix_shell_path=s.get("nix_shell_path", ""),
                    pending_nix_nudge=s.get("pending_nix_nudge", False),
                    extra_mounts=s.get("extra_mounts", []),
                    port_mappings=s.get("port_mappings", []),
                    acp_host=s.get("acp_host", ""),
                    acp_port=s.get("acp_port", 0),
                    acp_session_id=s.get("acp_session_id", ""),
                    acp_remote_cwd=s.get("acp_remote_cwd", ""),
                )
                self._sessions[session.id] = session
            log.info("Loaded %d persisted sessions", len(self._sessions))
        except Exception as e:
            log.warning("Failed to load sessions: %s", e)

    def save_sessions(self) -> None:
        """Persist sessions to disk (public — Router calls on shutdown)."""
        data = []
        for s in self._sessions.values():
            data.append({
                "id": s.id,
                "name": s.name,
                "room_id": s.room_id,
                "workspace_path": s.workspace_path,
                "socket_path": s.socket_path,
                "created_at": s.created_at,
                "status": s.status,
                "profile": s.profile,
                "image": s.image,
                "user_display_name": s.user_display_name,
                "user_pronouns": s.user_pronouns,
                "host_pid": s.host_pid,
                "container_id": s.container_id,
                "nix_shell_path": s.nix_shell_path,
                "pending_nix_nudge": s.pending_nix_nudge,
                "extra_mounts": s.extra_mounts,
                "port_mappings": s.port_mappings,
                "acp_host": s.acp_host,
                "acp_port": s.acp_port,
                "acp_session_id": s.acp_session_id,
                "acp_remote_cwd": s.acp_remote_cwd,
            })
        try:
            self._sessions_file.write_text(json.dumps(data, indent=2))
        except Exception as e:
            log.warning("Failed to save sessions: %s", e)

    # ── Queries ───────────────────────────────────────────────────────

    def get_session(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def get_session_by_room(self, room_id: str) -> Session | None:
        """Get a running session by its Matrix room ID."""
        for session in self._sessions.values():
            if session.room_id == room_id and session.status == "running":
                return session
        return None

    def get_any_session_by_room(self, room_id: str) -> Session | None:
        """Get any session (including stopped) by its Matrix room ID."""
        for session in self._sessions.values():
            if session.room_id == room_id:
                return session
        return None

    def list_sessions(self) -> list[Session]:
        return list(self._sessions.values())

    def active_sessions(self) -> list[Session]:
        return [s for s in self._sessions.values() if s.status == "running"]

    def sessions_needing_restore(self) -> list[Session]:
        return [s for s in self._sessions.values() if s.status == "was_running"]

    # ── Activity tracking ─────────────────────────────────────────────

    def touch_activity(self, session_id: str) -> None:
        """Record that a session had activity (resets idle timer)."""
        self._last_activity[session_id] = time.monotonic()

    def get_idle_seconds(self, session_id: str) -> float | None:
        """Seconds since last activity, or None if never active."""
        ts = self._last_activity.get(session_id)
        if ts is None:
            return None
        return time.monotonic() - ts

    def clear_activity(self, session_id: str) -> None:
        self._last_activity.pop(session_id, None)

    # ── Lifecycle: Create ─────────────────────────────────────────────

    async def create_session(
        self,
        name: str,
        room_id: str,
        socket_path: str,
        profile: str = "",
        user_display_name: str = "",
        user_pronouns: str = "",
    ) -> Session:
        """Create a new session (workspace, session dir, persistence)."""
        session_id = f"{_slugify(name)}-{uuid.uuid4().hex[:8]}"

        resolved_profile = profile or self.config.default_profile
        profile_obj = self.config.get_profile(resolved_profile)

        workspace = Path(self.config.workspace_base) / session_id
        workspace.mkdir(parents=True, exist_ok=True)

        state_dir = Path(self.config.session_base) / session_id
        state_dir.mkdir(parents=True, exist_ok=True)

        session = Session(
            id=session_id,
            name=name,
            room_id=room_id,
            workspace_path=str(workspace),
            socket_path=socket_path,
            profile=resolved_profile,
            image=profile_obj.image,
            user_display_name=user_display_name,
            user_pronouns=user_pronouns,
        )

        self._sessions[session_id] = session
        self.save_sessions()
        if self.audit:
            self.audit.log("session_created", session_id=session_id, name=name)
        log.info("Session created: %s (%s) profile=%s image=%s",
                 session_id, name, resolved_profile, profile_obj.image)
        return session

    # ── Lifecycle: Start ──────────────────────────────────────────────

    async def start_session(self, session_id: str) -> tuple[bool, str]:
        """Start a session's runtime (container or host process)."""
        session = self._sessions.get(session_id)
        if session is None:
            return False, "Session not found"

        # Delegate to RuntimeManager — it handles podman/host start
        ok, error = await self.runtime.start_session(session)
        self.save_sessions()

        if ok:
            self.touch_activity(session_id)

        return ok, error

    # ── Lifecycle: Stop ───────────────────────────────────────────────

    async def stop_session(
        self, session_id: str, *, reason: str = "unknown"
    ) -> bool:
        """Stop a session's runtime.  Notifies Matrix and cleans up IPC."""
        session = self._sessions.get(session_id)
        if session is None:
            log.error("Session not found: %s", session_id)
            return False

        log.info("Stopping session %s (reason=%s)", session_id, reason)
        session.status = "stopping"
        self.save_sessions()

        # Snapshot SDK state before tearing down the runtime
        await asyncio.to_thread(self.backup_sdk_state, session)

        # Send SHUTDOWN to agent if connected
        if self.ipc and self.ipc.is_connected(session_id):
            from enclave.orchestrator.ipc import Message, MessageType
            await self.ipc.send_to(
                session_id,
                Message(type=MessageType.SHUTDOWN, payload={}),
            )

        # Stop the runtime (container or host process)
        await self.runtime.stop_runtime(session)

        session.status = "stopped"
        self.save_sessions()
        log.info("Session stopped: %s (reason=%s)", session_id, reason)
        return True

    # ── Lifecycle: Delete ─────────────────────────────────────────────

    async def delete_session(
        self, session_id: str, *, reason: str = "delete"
    ) -> bool:
        """Fully remove a session: stop, clean up all resources, leave room."""
        session = self._sessions.get(session_id)
        if session is None:
            log.error("Session not found for deletion: %s", session_id)
            return False

        room_id = session.room_id

        # Stop if still running
        if session.status in ("running", "starting"):
            await self.stop_session(session_id, reason=reason)
        elif session.status == "stopping":
            await asyncio.sleep(2)

        # Clean up workspace directory
        if session.workspace_path:
            ws = Path(session.workspace_path)
            if ws.exists():
                shutil.rmtree(ws, ignore_errors=True)
                log.info("Removed workspace: %s", ws)

        # Clean up session state directory
        session_dir = Path(self.config.session_base) / session_id
        if session_dir.exists():
            shutil.rmtree(session_dir, ignore_errors=True)
            log.info("Removed session dir: %s", session_dir)

        # Remove IPC socket
        if self.ipc:
            await self.ipc.remove_socket(session_id)

        # Leave and forget Matrix room
        if self.matrix and room_id:
            await self.matrix.cleanup_room(room_id, reason="Session deleted")

        # Remove from memory and persist
        self._sessions.pop(session_id, None)
        self.clear_activity(session_id)
        self.save_sessions()

        if self.audit:
            self.audit.log("session_deleted", session_id=session_id, reason=reason)
        log.info("Session deleted: %s (reason=%s)", session_id, reason)
        return True

    # ── Lifecycle: Restore ────────────────────────────────────────────

    async def restore_session(self, session_id: str) -> tuple[bool, str]:
        """Restore a previously-running session (re-create IPC + start)."""
        session = self._sessions.get(session_id)
        if session is None:
            return False, "Session not found"

        session.status = "stopped"  # Reset before starting

        # Re-create IPC socket
        if self.ipc:
            socket_path = await self.ipc.create_socket(session_id)
            session.socket_path = str(socket_path)

        ok, error = await self.start_session(session_id)
        if ok and self.audit:
            self.audit.log("session_restored", session_id=session_id)
        return ok, error

    # ── Health check ──────────────────────────────────────────────────

    async def check_health(self) -> list[Session]:
        """Check running sessions and mark crashed ones as stopped."""
        crashed: list[Session] = []
        for session in list(self._sessions.values()):
            if session.status != "running":
                continue

            alive = await self.runtime.is_alive(session)
            if not alive:
                log.warning(
                    "Session %s runtime no longer alive — marking stopped",
                    session.id,
                )
                session.status = "stopped"
                session.host_pid = None
                crashed.append(session)

        if crashed:
            self.save_sessions()
        return crashed

    # ── Agent disconnect handler ──────────────────────────────────────

    async def on_agent_disconnect(self, session_id: str) -> None:
        """Handle agent IPC disconnect — notify Matrix appropriately."""
        session = self._sessions.get(session_id)
        if not session:
            log.info("Agent disconnected (no session): %s", session_id)
            return

        if session.status == "stopping":
            log.info("Agent disconnected (graceful stop): %s", session_id)
            if self.matrix:
                await self.matrix.send_message(
                    session.room_id, "🛑 Session stopped."
                )
        elif self._shutting_down:
            log.info("Agent disconnected (orchestrator shutdown): %s", session_id)
        elif session.status == "running":
            log.warning("Agent disconnected unexpectedly: %s", session_id)
            if self.matrix:
                await self.matrix.send_message(
                    session.room_id, "⚠️ Agent disconnected unexpectedly."
                )
        else:
            log.info("Agent disconnected (status=%s): %s", session.status, session_id)

        if self.audit:
            self.audit.log("agent_disconnected", session_id=session_id)

    # ── SDK state backups ─────────────────────────────────────────────

    _BACKUP_INTERVAL = 900  # 15 minutes
    _BACKUP_KEEP = 3        # rolling window
    _BACKUP_MIN_GAP = 30    # don't backup more often than this (seconds)

    def backup_sdk_state(self, session: Session, *, periodic: bool = False) -> bool:
        """Snapshot .copilot-state/ for a session. Runs in-thread (sync I/O).

        Returns True if a backup was created, False if skipped/failed.
        """
        ws = Path(session.workspace_path)
        src = ws / ".copilot-state"
        if not src.exists():
            return False

        backup_root = ws / ".copilot-backups"
        backup_root.mkdir(exist_ok=True)

        # Skip if the newest backup is very recent
        existing = sorted(backup_root.iterdir(), reverse=True) if backup_root.exists() else []
        if existing:
            newest_age = time.time() - existing[0].stat().st_mtime
            if newest_age < self._BACKUP_MIN_GAP:
                return False

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = backup_root / stamp

        try:
            shutil.copytree(src, dest)
            label = "periodic" if periodic else "turn-end"
            log.info("SDK state backed up (%s): %s → %s", label, session.id, dest.name)
        except Exception as e:
            log.error("SDK backup failed for %s: %s", session.id, e)
            return False

        # Prune old backups (keep newest N)
        backups = sorted(backup_root.iterdir(), reverse=True)
        for old in backups[self._BACKUP_KEEP:]:
            shutil.rmtree(old, ignore_errors=True)
            log.debug("Pruned old backup: %s", old.name)

        return True

    async def backup_all_running(self) -> int:
        """Backup SDK state for all running sessions. Returns count."""
        count = 0
        for session in self.active_sessions():
            if not session.workspace_path:
                continue
            ok = await asyncio.to_thread(
                self.backup_sdk_state, session, periodic=True,
            )
            if ok:
                count += 1
        return count
