"""Container manager for Enclave agent sessions.

Manages podman container lifecycle: create, start, stop, list.
Handles workspace setup and mount propagation.
"""

from __future__ import annotations

import asyncio
import functools
import json
import os
import signal
import subprocess
import sys
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
    profile: str = ""  # container profile name (e.g., "dev", "light")
    image: str = ""  # resolved container image for this session
    user_display_name: str = ""
    user_pronouns: str = ""
    host_pid: int | None = None  # PID for host-mode subprocess agents


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
                # Preserve the status that was saved — "was_running" means
                # it was running when we last saved state (e.g., before reboot)
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
                "status": s.status,
                "profile": s.profile,
                "image": s.image,
                "user_display_name": s.user_display_name,
                "user_pronouns": s.user_pronouns,
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

    def sessions_needing_restore(self) -> list[Session]:
        """List sessions that were running before last shutdown."""
        return [s for s in self._sessions.values() if s.status == "was_running"]

    async def create_session(
        self,
        name: str,
        room_id: str,
        socket_path: str,
        profile: str = "",
        user_display_name: str = "",
        user_pronouns: str = "",
    ) -> Session:
        """Create a new agent session with workspace and container.

        Args:
            name: Human-readable session name.
            room_id: Matrix room ID for this session.
            socket_path: Path to the IPC socket for this session.
            profile: Container profile name (e.g., "dev", "light").
                     Empty string uses the default profile.
            user_display_name: Display name of the user who owns this session.
            user_pronouns: Pronouns of the user (e.g., "he/him").

        Returns:
            The created Session object.
        """
        session_id = f"{_slugify(name)}-{uuid.uuid4().hex[:8]}"

        # Resolve profile and image
        resolved_profile = profile or self.config.default_profile
        profile_obj = self.config.get_profile(resolved_profile)

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
            profile=resolved_profile,
            image=profile_obj.image,
            user_display_name=user_display_name,
            user_pronouns=user_pronouns,
        )

        self._sessions[session_id] = session
        self._save_sessions()
        log.info("Session created: %s (%s) profile=%s image=%s",
                 session_id, name, resolved_profile, profile_obj.image)
        return session

    async def start_session(self, session_id: str) -> tuple[bool, str]:
        """Start the podman container for a session.

        Returns (success, error_detail) tuple.
        """
        session = self._sessions.get(session_id)
        if session is None:
            log.error("Session not found: %s", session_id)
            return False, "Session not found"

        session.status = "starting"
        socket_dir = str(Path(session.socket_path).parent)

        # Resolve profile settings for this session
        profile = self.config.get_profile(session.profile)

        # ── Host mode: spawn subprocess instead of container ──
        # An explicit empty image in the profile means host mode (no container).
        if profile.image == "":
            return await self._start_host_session(session, profile)

        # Clean up any leftover container with the same name
        log.info("[start:%s] Cleaning up old container...", session_id)
        stop_result = await _run_command(
            [self.config.runtime, "stop", "-t", "5", session_id], timeout=15.0
        )
        log.debug("stop result: rc=%d stderr=%s", stop_result.returncode, stop_result.stderr[:100])
        rm_result = await _run_command(
            [self.config.runtime, "rm", "-f", session_id], timeout=10.0
        )
        log.debug("rm result: rc=%d stderr=%s", rm_result.returncode, rm_result.stderr[:100])
        log.info("[start:%s] Cleanup done, building run command...", session_id)

        # Select network mode:
        # - Copilot SDK containers need network for API access
        # - Default containers are network-isolated
        has_copilot = bool(self.config.github_token)
        network = self.config.copilot_network if has_copilot else self.config.network

        # Use session-specific image, falling back to profile → global default
        image = session.image or profile.image or self.config.image

        cmd = [
            self.config.runtime, "run",
            "--detach",
            "--rm",
            "--name", session_id,
            "--userns", self.config.userns,
            "--network", network,
            # Workspace with rslave propagation so host-side mounts appear in container
            "-v", f"{session.workspace_path}:/workspace:rslave",
            "-v", f"{socket_dir}:/socket:Z",
            "-e", f"IPC_SOCKET=/socket/{Path(session.socket_path).name}",
            "-e", f"SESSION_ID={session_id}",
            "-e", f"SESSION_NAME={session.name}",
        ]

        # Nix store mount (only for profiles that use nix)
        if profile.nix_store:
            nix_store = Path(self.config.nix_store)
            nix_store.mkdir(parents=True, exist_ok=True)
            cmd.extend(["-v", f"{nix_store}:/nix"])

        # Bind-mount host paths read-only at /host/<path> (only for profiles that use host mounts)
        if profile.host_mounts:
            for host_path in self.config.host_mounts:
                if Path(host_path).exists():
                    container_path = f"/host{host_path}"
                    cmd.extend(["-v", f"{host_path}:{container_path}:ro"])

        # Build PATH — include nix and host dirs only when enabled
        path_parts = ["/usr/local/bin", "/usr/bin", "/bin"]
        if profile.nix_store:
            path_parts.insert(0, "/nix/var/nix/profiles/default/bin")
            cmd.extend(["-e", "NIX_PATH=nixpkgs=channel:nixpkgs-unstable"])
        if profile.host_mounts:
            host_bin_dirs = ["/host/usr/bin", "/host/usr/games", "/host/usr/local/bin"]
            path_parts.extend(host_bin_dirs)
            # GCC toolchain: tell gcc where to find cc1, as, ld etc.
            compiler_path = (
                "/host/usr/libexec/gcc/x86_64-linux-gnu/13"
                ":/host/usr/lib/gcc/x86_64-linux-gnu/13"
                ":/host/usr/bin"
            )
            # LIBRARY_PATH is used by the linker at compile/link time (not runtime).
            # We intentionally do NOT set LD_LIBRARY_PATH to avoid loading the
            # host's glibc into container processes (version mismatch → crash).
            link_lib_dirs = (
                "/host/usr/lib:/host/usr/lib/x86_64-linux-gnu"
                ":/host/usr/local/lib"
            )
            cmd.extend([
                "-e", f"COMPILER_PATH={compiler_path}",
                "-e", f"LIBRARY_PATH={link_lib_dirs}",
                "-e", f"C_INCLUDE_PATH=/host/usr/include",
                "-e", f"CPLUS_INCLUDE_PATH=/host/usr/include",
            ])

        cmd.extend(["-e", f"PATH={':'.join(path_parts)}"])

        # Custom DNS resolver (restricts what the container can resolve)
        if self.config.dns and network != "none":
            cmd.extend(["--dns", self.config.dns])

        # Pass GitHub token for Copilot SDK auth
        if self.config.github_token:
            cmd.extend(["-e", f"GITHUB_TOKEN={self.config.github_token}"])

        # Pass profile info so the agent can adapt its behaviour
        cmd.extend([
            "-e", f"ENCLAVE_PROFILE={session.profile}",
            "-e", f"ENCLAVE_NIX_STORE={'1' if profile.nix_store else '0'}",
            "-e", f"ENCLAVE_HOST_MOUNTS={'1' if profile.host_mounts else '0'}",
            "-e", f"ENCLAVE_YOLO={'1' if profile.yolo else '0'}",
        ])

        # Pass user identity so the agent can address them by name
        if session.user_display_name:
            cmd.extend(["-e", f"ENCLAVE_USER_NAME={session.user_display_name}"])
        if session.user_pronouns:
            cmd.extend(["-e", f"ENCLAVE_USER_PRONOUNS={session.user_pronouns}"])

        cmd.append(image)

        log.debug("Container cmd: %s", " ".join(cmd[:8]) + " ...")
        log.info("[start:%s] Executing podman run (profile=%s image=%s)...",
                 session_id, session.profile, image)

        try:
            # First container run after image build can take ~35s (overlay setup).
            # Use generous timeout to avoid killing it and corrupting storage.
            result = await _run_command(cmd, timeout=120.0)
            log.debug("Container run result: rc=%d stdout=%s stderr=%s",
                       result.returncode, result.stdout[:40], result.stderr[:100])
            if result.returncode == 0:
                container_id = result.stdout.strip()
                session.container_id = container_id
                session.status = "running"
                log.info(
                    "[start:%s] Container ready (id: %s)",
                    session_id,
                    container_id[:12],
                )
                return True, ""
            else:
                session.status = "stopped"
                stderr = result.stderr.strip()
                log.error(
                    "[start:%s] Container start failed: %s",
                    session_id,
                    stderr,
                )
                return False, _classify_container_error(stderr, image)
        except Exception as e:
            session.status = "stopped"
            log.error("[start:%s] Exception starting container: %s", session_id, e)
            return False, f"Exception: {e}"

    async def _start_host_session(
        self, session: Session, profile: "ContainerProfile"
    ) -> tuple[bool, str]:
        """Start an agent as a host subprocess (no container).

        Passes all required environment to the subprocess. Landlock
        sandboxing is applied inside the agent process at startup.
        """
        log.info("[start:%s] Starting in host mode (no container)", session.id)

        env = os.environ.copy()
        env["IPC_SOCKET"] = session.socket_path
        env["SESSION_ID"] = session.id
        env["SESSION_NAME"] = session.name
        env["ENCLAVE_PROFILE"] = session.profile
        env["ENCLAVE_NIX_STORE"] = "1" if profile.nix_store else "0"
        env["ENCLAVE_HOST_MOUNTS"] = "0"
        env["ENCLAVE_YOLO"] = "1" if profile.yolo else "0"
        env["ENCLAVE_HOST_MODE"] = "1"

        if self.config.github_token:
            env["GITHUB_TOKEN"] = self.config.github_token
            log.debug("[start:%s] Passing GITHUB_TOKEN to host agent", session.id)
        else:
            log.warning("[start:%s] No github_token configured — host agent will lack Copilot SDK", session.id)

        if session.user_display_name:
            env["ENCLAVE_USER_NAME"] = session.user_display_name
        if session.user_pronouns:
            env["ENCLAVE_USER_PRONOUNS"] = session.user_pronouns

        # Landlock config: pass workspace path so agent can apply sandbox
        env["ENCLAVE_WORKSPACE"] = session.workspace_path

        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "enclave.agent.main",
                env=env,
                cwd=session.workspace_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            session.host_pid = proc.pid
            session.status = "running"
            log.info(
                "[start:%s] Host agent started (pid: %d)",
                session.id, proc.pid,
            )

            # Start background task to log output and detect exit
            asyncio.create_task(self._monitor_host_process(session, proc))

            return True, ""
        except Exception as e:
            session.status = "stopped"
            log.error("[start:%s] Failed to start host agent: %s", session.id, e)
            return False, f"Host mode start failed: {e}"

    async def _monitor_host_process(
        self, session: Session, proc: asyncio.subprocess.Process
    ) -> None:
        """Monitor a host-mode agent subprocess, streaming stderr to log."""
        try:
            # Stream stderr in real time so we can see SDK init messages
            async def _stream_stderr():
                assert proc.stderr is not None
                async for line in proc.stderr:
                    text = line.decode().rstrip()
                    if text:
                        log.info("[host:%s] %s", session.id, text)

            stderr_task = asyncio.create_task(_stream_stderr())
            stdout, _ = await proc.communicate()
            await stderr_task
            if stdout:
                log.info("[host:%s] stdout: %s", session.id, stdout.decode()[-500:])
            if proc.returncode != 0:
                log.warning(
                    "[host:%s] Agent exited with code %d",
                    session.id, proc.returncode,
                )
            session.status = "stopped"
            session.host_pid = None
        except Exception as e:
            log.error("[host:%s] Error monitoring process: %s", session.id, e)

    async def stop_session(self, session_id: str) -> bool:
        """Stop and remove a session's container or host process.

        Returns True on success.
        """
        session = self._sessions.get(session_id)
        if session is None:
            log.error("Session not found: %s", session_id)
            return False

        session.status = "stopping"

        # Host mode: kill the subprocess
        if session.host_pid:
            try:
                os.kill(session.host_pid, signal.SIGTERM)
                # Wait briefly for graceful shutdown
                await asyncio.sleep(2)
                try:
                    os.kill(session.host_pid, 0)  # Check if still alive
                    os.kill(session.host_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass  # Already exited
            except ProcessLookupError:
                pass  # Already dead
            except Exception as e:
                log.warning("Error stopping host process %d: %s", session.host_pid, e)
            session.host_pid = None
        elif session.container_id:
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

    async def check_health(self) -> list[Session]:
        """Check all 'running' sessions and mark crashed ones as stopped.

        Returns a list of sessions that were found to be crashed.
        """
        crashed: list[Session] = []
        for session in list(self._sessions.values()):
            if session.status != "running":
                continue

            # Host mode: check if PID is still alive
            if session.host_pid:
                try:
                    os.kill(session.host_pid, 0)
                except ProcessLookupError:
                    log.warning(
                        "Session %s host process (pid %d) no longer running — "
                        "marking stopped",
                        session.id, session.host_pid,
                    )
                    session.status = "stopped"
                    session.host_pid = None
                    crashed.append(session)
                continue

            actual = await self.get_container_status(session.id)
            if actual is None or actual not in ("running", "created"):
                log.warning(
                    "Session %s marked running but container status is %s — "
                    "marking stopped",
                    session.id,
                    actual,
                )
                session.status = "stopped"
                crashed.append(session)
        if crashed:
            self._save_sessions()
        return crashed


@dataclass
class CommandResult:
    """Result of running a shell command."""

    returncode: int
    stdout: str
    stderr: str


def _run_command_sync(cmd: list[str], timeout: float) -> CommandResult:
    """Run a command synchronously (for use in thread pool)."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return CommandResult(
            returncode=result.returncode,
            stdout=result.stdout or "",
            stderr=result.stderr or "",
        )
    except subprocess.TimeoutExpired:
        return CommandResult(returncode=-1, stdout="", stderr="timeout")


async def _run_command(cmd: list[str], timeout: float = 30.0) -> CommandResult:
    """Run a command in a thread pool to avoid event loop deadlocks."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, functools.partial(_run_command_sync, cmd, timeout)
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


def _classify_container_error(stderr: str, image: str) -> str:
    """Turn raw podman stderr into a human-friendly error message."""
    lower = stderr.lower()
    if "did not resolve to an alias" in lower or "image not known" in lower:
        return f"Container image `{image}` not found. Has it been built?"
    if "address already in use" in lower:
        return "Port conflict — another container may already be using it."
    if "no space left on device" in lower:
        return "Disk full — not enough space to start the container."
    if "permission denied" in lower:
        return "Permission denied — check podman rootless setup."
    if "timeout" in lower:
        return "Container start timed out."
    # Fall back to the raw error, trimmed
    return stderr[:200]
