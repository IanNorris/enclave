"""Runtime manager for Enclave agent sessions.

Handles the low-level podman container and host-process operations.
Session state and lifecycle orchestration live in SessionManager.
"""

from __future__ import annotations

import asyncio
import functools
import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from enclave.common.config import ContainerConfig, MimirConfig
from enclave.common.logging import get_logger

# Re-export Session so existing imports keep working
from enclave.orchestrator.session_manager import Session  # noqa: F401

log = get_logger("container")


class ContainerManager:
    """Low-level runtime operations for containers and host processes.

    This class knows how to start/stop podman containers and host-mode
    subprocesses.  It does NOT own sessions or persist state — that is
    the responsibility of SessionManager.
    """

    def __init__(self, config: ContainerConfig, *, mimir: MimirConfig | None = None):
        self.config = config
        self.mimir = mimir

    async def start_session(self, session: Session) -> tuple[bool, str]:
        """Start the runtime for a session (container or host process).

        Returns (success, error_detail) tuple.
        """
        session.status = "starting"
        socket_dir = str(Path(session.socket_path).parent)

        profile = self.config.get_profile(session.profile)

        # Host mode: spawn subprocess instead of container
        if profile.image == "":
            return await self._start_host_session(session, profile)

        # Clean up any leftover container with the same name
        log.info("[start:%s] Cleaning up old container...", session.id)
        stop_result = await _run_command(
            [self.config.runtime, "stop", "-t", "5", session.id], timeout=15.0
        )
        log.debug("stop result: rc=%d stderr=%s", stop_result.returncode, stop_result.stderr[:100])
        rm_result = await _run_command(
            [self.config.runtime, "rm", "-f", session.id], timeout=10.0
        )
        log.debug("rm result: rc=%d stderr=%s", rm_result.returncode, rm_result.stderr[:100])
        log.info("[start:%s] Cleanup done, building run command...", session.id)

        # Select network mode:
        # - Copilot SDK containers need network for API access
        # - Sessions with port mappings need network for published ports
        # - Default containers are network-isolated
        has_copilot = bool(self.config.github_token)
        has_ports = bool(session.port_mappings)
        if has_ports:
            network = self.config.port_network
        elif has_copilot:
            network = self.config.copilot_network
        else:
            network = self.config.network

        # Use session-specific image, falling back to profile → global default
        image = session.image or profile.image or self.config.image

        cmd = [
            self.config.runtime, "run",
            "--detach",
            "--rm",
            "--name", session.id,
            "--userns", self.config.userns,
            "--network", network,
            # Workspace mount
            "-v", f"{session.workspace_path}:/workspace",
            "-v", f"{socket_dir}:/socket:Z",
            "-e", f"IPC_SOCKET=/socket/{Path(session.socket_path).name}",
            "-e", f"SESSION_ID={session.id}",
            "-e", f"SESSION_NAME={session.name}",
        ]

        # Publish port mappings
        bind_addr = self.config.port_bind_address
        for pm in session.port_mappings:
            cp = pm["container_port"]
            hp = pm["host_port"]
            proto = pm.get("protocol", "tcp")
            cmd.extend(["-p", f"{bind_addr}:{hp}:{cp}/{proto}"])

        # Nix store mount (only for profiles that use nix)
        if profile.nix_store:
            nix_store = Path(self.config.nix_store)
            nix_store.mkdir(parents=True, exist_ok=True)
            cmd.extend(["-v", f"{nix_store}:/nix"])

        # Mimir memory backend: bind-mount the per-agent workspace at the
        # same path layout the host uses, and forward config to the agent
        # via env. Workspace contains canonical.log + drafts/. Disabled by
        # default (mimir.enabled=False); when disabled we only export the
        # ENCLAVE_MIMIR_ENABLED=0 flag so the agent's killswitch trips
        # cleanly without needing to know the workspace path.
        if self.mimir is not None and self.mimir.enabled:
            agent_name = self.mimir.agent_name or "brook"
            # Defence-in-depth path validation: enforce alnum+_- and a
            # resolved path strictly under workspace_root, so a misconfig
            # can't escape into arbitrary host paths.
            if not all(c.isalnum() or c in "-_" for c in agent_name):
                log.warning(
                    "[start:%s] Invalid mimir.agent_name=%r — disabling Mimir for this session",
                    session.id, agent_name,
                )
            else:
                root = Path(self.mimir.workspace_root).expanduser().resolve()
                ws = (root / agent_name).resolve()
                # ws must live strictly under root.
                try:
                    ws.relative_to(root)
                    valid_path = True
                except ValueError:
                    valid_path = False
                if not valid_path:
                    log.warning(
                        "[start:%s] Mimir workspace %s escapes root %s — disabling",
                        session.id, ws, root,
                    )
                else:
                    ws.mkdir(parents=True, exist_ok=True)
                    (ws / "drafts" / "pending").mkdir(parents=True, exist_ok=True)
                    container_ws = f"/home/agent/.local/share/enclave/mimir/{agent_name}"
                    cmd.extend([
                        "-v", f"{ws}:{container_ws}",
                        "-e", "ENCLAVE_MIMIR_ENABLED=1",
                        "-e", f"ENCLAVE_MIMIR_AGENT_NAME={agent_name}",
                        "-e", f"ENCLAVE_MIMIR_WORKSPACE_ROOT=/home/agent/.local/share/enclave/mimir",
                        "-e", f"ENCLAVE_MIMIR_MCP_BIN={self.mimir.mcp_bin}",
                        "-e", f"ENCLAVE_MIMIR_CLI_BIN={self.mimir.cli_bin}",
                        "-e", f"ENCLAVE_MIMIR_LIBRARIAN_BIN={self.mimir.librarian_bin}",
                    ])
                    log.info("[start:%s] Mimir enabled, workspace=%s", session.id, ws)
        else:
            cmd.extend(["-e", "ENCLAVE_MIMIR_ENABLED=0"])

        # Extra mounts requested by the agent (approved by user)
        for mount in getattr(session, "extra_mounts", []):
            source = mount.get("source", "")
            mount_name = mount.get("mount_name", "")
            if source and mount_name and Path(source).exists():
                cmd.extend(["-v", f"{source}:/workspace/{mount_name}:ro"])
                log.info("[start:%s] Extra mount: %s → /workspace/%s", session.id, source, mount_name)

        # Bind-mount host paths read-only at /host/<path> (only for profiles that use host mounts)
        if profile.host_mounts:
            for host_path in self.config.host_mounts:
                if Path(host_path).exists():
                    container_path = f"/host{host_path}"
                    cmd.extend(["-v", f"{host_path}:{container_path}:ro"])

        # FUSE: user-space mounts (e.g. mounting disk images without root).
        # Needs /dev/fuse device + SYS_ADMIN cap inside the userns. The cap
        # is scoped to the rootless user namespace, so this does not grant
        # host-level privileges — mounts are only visible to this container.
        if profile.fuse:
            fuse_dev = Path("/dev/fuse")
            if fuse_dev.exists():
                cmd.extend([
                    "--device", "/dev/fuse",
                    "--cap-add", "SYS_ADMIN",
                    "--security-opt", "apparmor=unconfined",
                ])
                log.info(
                    "[start:%s] FUSE enabled (/dev/fuse + SYS_ADMIN)",
                    session.id,
                )
            else:
                log.warning(
                    "[start:%s] Profile requests FUSE but /dev/fuse missing "
                    "on host — skipping", session.id,
                )

        # GUI passthrough: mount Wayland socket + GPU for direct rendering
        if profile.gui:
            xdg = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
            wayland = os.environ.get("WAYLAND_DISPLAY", "wayland-0")
            wayland_sock = Path(xdg) / wayland
            if wayland_sock.exists():
                cmd.extend([
                    "-v", f"{wayland_sock}:/run/user/1000/{wayland}:rw",
                    "-e", f"WAYLAND_DISPLAY={wayland}",
                    "-e", f"XDG_RUNTIME_DIR=/run/user/1000",
                ])
                log.info("[start:%s] Wayland socket mounted: %s", session.id, wayland)
            # GPU device for hardware-accelerated rendering
            dri = Path("/dev/dri")
            if dri.exists():
                cmd.extend(["--device", "/dev/dri"])
                log.info("[start:%s] GPU device mounted", session.id)
            # KVM device for hardware-accelerated virtualisation (QEMU etc.)
            kvm = Path("/dev/kvm")
            if kvm.exists():
                cmd.extend(["--device", "/dev/kvm"])
                log.info("[start:%s] KVM device mounted", session.id)

            # NixOS GPU driver stack (Mesa, EGL, Vulkan)
            opengl_driver = Path("/run/opengl-driver")
            if opengl_driver.exists():
                real_driver = opengl_driver.resolve()
                cmd.extend([
                    "-v", f"{real_driver}:/run/opengl-driver:ro",
                    "-e", "LIBGL_DRIVERS_PATH=/run/opengl-driver/lib/dri",
                    "-e", "__EGL_VENDOR_LIBRARY_DIRS=/run/opengl-driver/share/glvnd/egl_vendor.d",
                ])
                ld_paths = ["/run/opengl-driver/lib"]
                log.info("[start:%s] NixOS GPU drivers mounted: %s", session.id, real_driver)
                # libglvnd provides the GL dispatch layer (libEGL.so.1, libGL.so.1)
                # that apps link against — Mesa only has vendor libs (libEGL_mesa.so)
                glvnd_path = self._find_libglvnd(real_driver)
                if glvnd_path:
                    cmd.extend(["-v", f"{glvnd_path}:/run/libglvnd:ro"])
                    ld_paths.insert(0, "/run/libglvnd/lib")
                    log.info("[start:%s] libglvnd mounted: %s", session.id, glvnd_path)
                cmd.extend(["-e", f"LD_LIBRARY_PATH={':'.join(ld_paths)}"])

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

        # Nix-shell wrapping: agent requested a specific nix environment
        if session.nix_shell_path:
            # Path is stored as the container-relative path (e.g. /workspace/shell.nix)
            cmd.extend(["-e", f"ENCLAVE_NIX_SHELL={session.nix_shell_path}"])

        cmd.append(image)

        log.debug("Container cmd: %s", " ".join(cmd[:8]) + " ...")
        log.info("[start:%s] Executing podman run (profile=%s image=%s)...",
                 session.id, session.profile, image)

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
                    session.id,
                    container_id[:12],
                )
                return True, ""
            else:
                session.status = "stopped"
                stderr = result.stderr.strip()
                log.error(
                    "[start:%s] Container start failed: %s",
                    session.id,
                    stderr,
                )
                return False, _classify_container_error(stderr, image)
        except Exception as e:
            session.status = "stopped"
            log.error("[start:%s] Exception starting container: %s", session.id, e)
            return False, f"Exception: {e}"

    @staticmethod
    def _find_libglvnd(driver_path: Path) -> Path | None:
        """Find the libglvnd store path from the NixOS graphics driver closure."""
        try:
            result = subprocess.run(
                ["nix-store", "-q", "--requisites", str(driver_path)],
                capture_output=True, text=True, timeout=10,
            )
            for line in result.stdout.splitlines():
                p = Path(line.strip())
                if "libglvnd" in p.name and (p / "lib" / "libEGL.so.1").exists():
                    return p
        except Exception as e:
            log.warning("Failed to find libglvnd: %s", e)
        return None

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

    async def stop_runtime(self, session: Session) -> None:
        """Stop the runtime (container or host process) for a session.

        Does NOT update session status — that is SessionManager's job.
        """
        if session.host_pid:
            try:
                os.kill(session.host_pid, signal.SIGTERM)
                await asyncio.sleep(2)
                try:
                    os.kill(session.host_pid, 0)
                    os.kill(session.host_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            except ProcessLookupError:
                pass
            except Exception as e:
                log.warning("Error stopping host process %d: %s", session.host_pid, e)
            session.host_pid = None
        elif session.container_id:
            try:
                await _run_command(
                    [self.config.runtime, "stop", "-t", "10", session.id]
                )
            except Exception as e:
                log.warning("Error stopping container %s: %s", session.id, e)

    async def is_alive(self, session: Session) -> bool:
        """Check whether a session's runtime is still running."""
        if session.host_pid:
            try:
                os.kill(session.host_pid, 0)
                return True
            except ProcessLookupError:
                return False

        actual = await self.get_container_status(session.id)
        return actual in ("running", "created")

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

    async def container_has_processes(self, session_id: str) -> bool:
        """Check if a container has non-trivial running processes."""
        try:
            result = await asyncio.create_subprocess_exec(
                self.config.runtime, "top", session_id,
                "--format", "{{.Command}}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(result.communicate(), timeout=5.0)
            lines = stdout.decode().strip().splitlines()
            skip = {"python", "python3", "node", "sleep", "bash", "sh", "tini"}
            for line in lines[1:]:  # skip header
                cmd = line.strip().split("/")[-1].split()[0]
                if cmd and cmd not in skip:
                    return True
        except Exception:
            pass
        return False


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
