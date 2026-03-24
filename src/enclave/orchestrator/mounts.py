"""Mount manager: grant/revoke filesystem access to agent containers.

Uses bind mounts with shared mount propagation to dynamically add/remove
paths from running containers. The workspace is pre-mounted with shared
propagation, so new bind mounts propagate into the container instantly.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from pathlib import Path

from enclave.common.logging import get_logger

log = get_logger("mounts")


@dataclass
class MountPoint:
    """An active mount inside a session workspace."""

    source: str        # Host path (e.g. /home/ian/projects/myapp)
    mount_name: str    # Name inside workspace (e.g. projects-myapp)
    session_id: str
    active: bool = True


class MountManager:
    """Manages filesystem mounts for agent sessions.

    Each session has a workspace directory. This manager bind-mounts
    approved paths into that workspace. With shared mount propagation,
    mounts appear inside running containers instantly.
    """

    def __init__(self, use_sudo: bool = True):
        self.use_sudo = use_sudo
        self._mounts: dict[str, list[MountPoint]] = {}  # session_id → mounts

    def _workspace_target(self, workspace: str, mount_name: str) -> str:
        """Get the full target path inside the workspace."""
        return str(Path(workspace) / mount_name)

    def _sanitize_name(self, path: str) -> str:
        """Convert a path to a safe mount name."""
        return (
            path.strip("/")
            .replace("/", "-")
            .replace(" ", "-")
            .replace("..", "")[:64]
        )

    async def grant_mount(
        self,
        session_id: str,
        workspace: str,
        source_path: str,
        mount_name: str | None = None,
    ) -> MountPoint | None:
        """Mount a host path into the session workspace.

        Args:
            session_id: The session to mount into.
            workspace: The session's workspace directory on the host.
            source_path: The host path to make available.
            mount_name: Optional name for the mount point inside workspace.

        Returns:
            MountPoint on success, None on failure.
        """
        source = Path(source_path)
        if not source.exists():
            log.error("Source path does not exist: %s", source_path)
            return None

        if mount_name is None:
            mount_name = self._sanitize_name(source_path)

        target = self._workspace_target(workspace, mount_name)
        os.makedirs(target, exist_ok=True)

        # Bind mount
        cmd = ["mount", "--bind", source_path, target]
        if self.use_sudo:
            cmd = ["sudo"] + cmd

        try:
            result = await _run(cmd)
            if result != 0:
                log.error("Mount failed: %s → %s", source_path, target)
                return None
        except Exception as e:
            log.error("Mount error: %s", e)
            return None

        mount = MountPoint(
            source=source_path,
            mount_name=mount_name,
            session_id=session_id,
        )
        self._mounts.setdefault(session_id, []).append(mount)
        log.info("Mounted %s → %s", source_path, target)
        return mount

    async def revoke_mount(
        self,
        session_id: str,
        workspace: str,
        mount_name: str,
    ) -> bool:
        """Unmount a path from the session workspace.

        Returns True on success.
        """
        target = self._workspace_target(workspace, mount_name)

        cmd = ["umount", target]
        if self.use_sudo:
            cmd = ["sudo"] + cmd

        try:
            result = await _run(cmd)
            if result != 0:
                log.warning("Unmount failed: %s", target)
                return False
        except Exception as e:
            log.error("Unmount error: %s", e)
            return False

        # Update tracking
        mounts = self._mounts.get(session_id, [])
        for m in mounts:
            if m.mount_name == mount_name:
                m.active = False
                break

        log.info("Unmounted %s", target)
        return True

    async def revoke_all(self, session_id: str, workspace: str) -> int:
        """Unmount all paths for a session. Returns count unmounted."""
        mounts = self._mounts.get(session_id, [])
        count = 0
        for m in mounts:
            if m.active:
                ok = await self.revoke_mount(session_id, workspace, m.mount_name)
                if ok:
                    count += 1
        return count

    def list_mounts(self, session_id: str) -> list[MountPoint]:
        """List active mounts for a session."""
        return [
            m for m in self._mounts.get(session_id, [])
            if m.active
        ]

    def has_mount(self, session_id: str, source_path: str) -> bool:
        """Check if a source path is already mounted for a session."""
        for m in self._mounts.get(session_id, []):
            if m.source == source_path and m.active:
                return True
        return False

    async def setup_shared_propagation(self, workspace: str) -> bool:
        """Set up shared mount propagation on a workspace directory.

        Must be called before starting the container. Makes the workspace
        a shared mount point so new bind mounts propagate into containers.
        """
        cmd1 = ["mount", "--bind", workspace, workspace]
        cmd2 = ["mount", "--make-shared", workspace]
        if self.use_sudo:
            cmd1 = ["sudo"] + cmd1
            cmd2 = ["sudo"] + cmd2

        try:
            r1 = await _run(cmd1)
            r2 = await _run(cmd2)
            if r1 == 0 and r2 == 0:
                log.info("Shared mount propagation set up: %s", workspace)
                return True
            log.error("Failed to set up propagation for %s", workspace)
            return False
        except Exception as e:
            log.error("Propagation setup error: %s", e)
            return False


async def _run(cmd: list[str]) -> int:
    """Run a shell command, return the exit code."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0 and stderr:
        log.debug("Command failed (%s): %s", cmd, stderr.decode().strip())
    return proc.returncode or 0
