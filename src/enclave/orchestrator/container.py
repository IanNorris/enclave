"""Container manager for Enclave agent sessions.

Manages podman container lifecycle: create, start, stop, list.
Handles workspace setup and mount propagation.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from enclave.common.config import ContainerConfig
from enclave.common.logging import get_logger

log = get_logger("container")


@dataclass
class Session:
    """Represents an active agent session."""

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


class ContainerManager:
    """Manages podman containers for agent sessions."""

    def __init__(self, config: ContainerConfig):
        self.config = config
        self._sessions: dict[str, Session] = {}
        self._sessions_file = Path(config.session_base) / "sessions.json"

        # Ensure base directories exist
        Path(config.workspace_base).mkdir(parents=True, exist_ok=True)
        Path(config.session_base).mkdir(parents=True, exist_ok=True)

        # Load persisted sessions
        self._load_sessions()

    def _load_sessions(self) -> None:
        """Load sessions from disk."""
        if not self._sessions_file.exists():
            return
        try:
            data = json.loads(self._sessions_file.read_text())
            for s in data:
                session = Session(
                    id=s["id"],
                    name=s["name"],
                    room_id=s["room_id"],
                    workspace_path=s.get("workspace_path", ""),
                    socket_path=s.get("socket_path", ""),
                    created_at=s.get("created_at", ""),
                    status="stopped",  # assume stopped on load
                )
                self._sessions[session.id] = session
            log.info("Loaded %d persisted sessions", len(self._sessions))
        except Exception as e:
            log.warning("Failed to load sessions: %s", e)

    def _save_sessions(self) -> None:
        """Persist sessions to disk."""
        data = []
        for s in self._sessions.values():
            data.append({
                "id": s.id,
                "name": s.name,
                "room_id": s.room_id,
                "workspace_path": s.workspace_path,
                "socket_path": s.socket_path,
                "created_at": s.created_at,
            })
        try:
            self._sessions_file.write_text(json.dumps(data, indent=2))
        except Exception as e:
            log.warning("Failed to save sessions: %s", e)

    def get_session(self, session_id: str) -> Session | None:
        """Get a session by ID."""
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
        """List all sessions."""
        return list(self._sessions.values())

    def active_sessions(self) -> list[Session]:
        """List running sessions."""
        return [s for s in self._sessions.values() if s.status == "running"]

    async def create_session(
        self,
        name: str,
        room_id: str,
        socket_path: str,
    ) -> Session:
        """Create a new agent session with workspace and container.

        Args:
            name: Human-readable session name.
            room_id: Matrix room ID for this session.
            socket_path: Path to the IPC socket for this session.

        Returns:
            The created Session object.
        """
        session_id = f"{_slugify(name)}-{uuid.uuid4().hex[:8]}"

        # Create workspace directory
        workspace = Path(self.config.workspace_base) / session_id
        workspace.mkdir(parents=True, exist_ok=True)

        # Create session state directory
        state_dir = Path(self.config.session_base) / session_id
        state_dir.mkdir(parents=True, exist_ok=True)

        session = Session(
            id=session_id,
            name=name,
            room_id=room_id,
            workspace_path=str(workspace),
            socket_path=socket_path,
        )

        self._sessions[session_id] = session
        self._save_sessions()
        log.info("Session created: %s (%s)", session_id, name)
        return session

    async def start_session(self, session_id: str) -> bool:
        """Start the podman container for a session.

        Returns True on success, False on failure.
        """
        session = self._sessions.get(session_id)
        if session is None:
            log.error("Session not found: %s", session_id)
            return False

        # Clean up any leftover container with the same name
        await _run_command(
            [self.config.runtime, "rm", "-f", session_id], timeout=10.0
        )

        session.status = "starting"
        socket_dir = str(Path(session.socket_path).parent)

        cmd = [
            self.config.runtime, "run",
            "--detach",
            "--rm",
            "--name", session_id,
            "--userns", self.config.userns,
            "--network", self.config.network,
            "-v", f"{session.workspace_path}:/workspace:Z",
            "-v", f"{socket_dir}:/socket:Z",
            "-e", f"IPC_SOCKET=/socket/{Path(session.socket_path).name}",
            "-e", f"SESSION_ID={session_id}",
            "-e", f"SESSION_NAME={session.name}",
            self.config.image,
        ]

        try:
            result = await _run_command(cmd)
            if result.returncode == 0:
                container_id = result.stdout.strip()
                session.container_id = container_id
                session.status = "running"
                log.info(
                    "Container started: %s (id: %s)",
                    session_id,
                    container_id[:12],
                )
                return True
            else:
                session.status = "stopped"
                log.error(
                    "Container start failed for %s: %s",
                    session_id,
                    result.stderr,
                )
                return False
        except Exception as e:
            session.status = "stopped"
            log.error("Failed to start container %s: %s", session_id, e)
            return False

    async def stop_session(self, session_id: str) -> bool:
        """Stop and remove a session's container.

        Returns True on success.
        """
        session = self._sessions.get(session_id)
        if session is None:
            log.error("Session not found: %s", session_id)
            return False

        session.status = "stopping"

        if session.container_id:
            try:
                await _run_command(
                    [self.config.runtime, "stop", "-t", "10", session_id]
                )
            except Exception as e:
                log.warning("Error stopping container %s: %s", session_id, e)

        session.status = "stopped"
        log.info("Session stopped: %s", session_id)
        return True

    async def remove_session(self, session_id: str) -> bool:
        """Remove a session entirely (stop container + clean up)."""
        await self.stop_session(session_id)
        session = self._sessions.pop(session_id, None)
        if session:
            log.info("Session removed: %s", session_id)
            return True
        return False

    async def get_container_status(self, session_id: str) -> str | None:
        """Check the actual podman status of a container."""
        try:
            result = await _run_command([
                self.config.runtime, "inspect",
                "--format", "{{.State.Status}}",
                session_id,
            ])
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return None


@dataclass
class CommandResult:
    """Result of running a shell command."""

    returncode: int
    stdout: str
    stderr: str


async def _run_command(cmd: list[str], timeout: float = 30.0) -> CommandResult:
    """Run a command asynchronously and return the result."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return CommandResult(returncode=-1, stdout="", stderr="timeout")

    return CommandResult(
        returncode=proc.returncode or 0,
        stdout=stdout.decode() if stdout else "",
        stderr=stderr.decode() if stderr else "",
    )


def _slugify(name: str) -> str:
    """Convert a name to a filesystem/container-safe slug."""
    return (
        name.lower()
        .replace(" ", "-")
        .replace("_", "-")
        .replace(".", "-")
        .strip("-")[:32]
    )
