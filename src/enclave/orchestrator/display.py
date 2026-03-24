"""Desktop integration: Hyprland session detection, GUI launching, screenshots.

Detects the active Hyprland session and provides methods to:
- Launch GUI applications on the user's desktop
- Take screenshots and send them to Matrix
- Fall back to headless mode (tmux) when no display is available
"""

from __future__ import annotations

import asyncio
import os
import json
from pathlib import Path

from enclave.common.logging import get_logger

log = get_logger("display")


class DisplayManager:
    """Manages desktop session detection and interaction.

    Supports Hyprland (primary) with headless fallback via tmux.
    """

    def __init__(self, user: str = ""):
        self.user = user or os.environ.get("USER", "")
        self._hyprland_socket: str | None = None
        self._display_available = False

    # ------------------------------------------------------------------
    # Session detection
    # ------------------------------------------------------------------

    def detect_session(self) -> bool:
        """Detect active Hyprland session.

        Checks for Hyprland socket and environment variables.
        Returns True if a desktop session is available.
        """
        # Check env var first
        instance = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
        if instance:
            socket_path = f"/tmp/hypr/{instance}/.socket.sock"
            if Path(socket_path).exists():
                self._hyprland_socket = socket_path
                self._display_available = True
                log.info("Hyprland session detected: %s", instance)
                return True

        # Scan for Hyprland sockets
        hypr_dir = Path("/tmp/hypr")
        if hypr_dir.exists():
            for instance_dir in hypr_dir.iterdir():
                sock = instance_dir / ".socket.sock"
                if sock.exists():
                    self._hyprland_socket = str(sock)
                    self._display_available = True
                    log.info("Found Hyprland socket: %s", sock)
                    return True

        # Check XDG_RUNTIME_DIR for Hyprland
        xdg = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        hypr_xdg = Path(xdg) / "hypr"
        if hypr_xdg.exists():
            for instance_dir in hypr_xdg.iterdir():
                sock = instance_dir / ".socket.sock"
                if sock.exists():
                    self._hyprland_socket = str(sock)
                    self._display_available = True
                    log.info("Found Hyprland socket (XDG): %s", sock)
                    return True

        self._display_available = False
        log.info("No desktop session detected")
        return False

    @property
    def is_available(self) -> bool:
        """Whether a desktop session is available."""
        return self._display_available

    @property
    def session_type(self) -> str:
        """Type of display session."""
        if self._hyprland_socket:
            return "hyprland"
        return "headless"

    # ------------------------------------------------------------------
    # Hyprland interaction
    # ------------------------------------------------------------------

    async def hyprctl(self, *args: str) -> str | None:
        """Run a hyprctl command and return stdout.

        Returns None if Hyprland is not available or command fails.
        """
        if not self._display_available:
            return None

        cmd = ["hyprctl"] + list(args)
        env = dict(os.environ)
        if self._hyprland_socket:
            # Extract instance signature from socket path
            parts = self._hyprland_socket.split("/")
            for i, p in enumerate(parts):
                if p == "hypr" and i + 1 < len(parts):
                    env["HYPRLAND_INSTANCE_SIGNATURE"] = parts[i + 1]
                    break

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode == 0:
                return stdout.decode().strip()
            log.warning("hyprctl failed: %s", stderr.decode().strip())
            return None
        except FileNotFoundError:
            log.warning("hyprctl not found")
            return None

    async def launch_app(self, command: str) -> bool:
        """Launch a GUI application on the Hyprland desktop.

        Args:
            command: The command to run (e.g., "firefox", "code .")

        Returns:
            True if launched successfully.
        """
        if not self._display_available:
            log.info("No desktop available, cannot launch GUI: %s", command)
            return False

        result = await self.hyprctl("dispatch", "exec", command)
        if result is not None:
            log.info("Launched GUI app: %s", command)
            return True
        return False

    async def get_active_window(self) -> dict | None:
        """Get info about the currently active window."""
        result = await self.hyprctl("activewindow", "-j")
        if result:
            try:
                return json.loads(result)
            except json.JSONDecodeError:
                pass
        return None

    async def list_windows(self) -> list[dict]:
        """List all windows."""
        result = await self.hyprctl("clients", "-j")
        if result:
            try:
                return json.loads(result)
            except json.JSONDecodeError:
                pass
        return []

    # ------------------------------------------------------------------
    # Screenshots
    # ------------------------------------------------------------------

    async def take_screenshot(self, output_path: str) -> bool:
        """Take a screenshot using grim.

        Args:
            output_path: Where to save the screenshot (PNG).

        Returns:
            True on success.
        """
        if not self._display_available:
            log.info("No desktop available for screenshot")
            return False

        try:
            proc = await asyncio.create_subprocess_exec(
                "grim", output_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode == 0:
                log.info("Screenshot saved: %s", output_path)
                return True
            log.warning("grim failed: %s", stderr.decode().strip())
            return False
        except FileNotFoundError:
            log.warning("grim not found — install grim for screenshots")
            return False

    async def take_region_screenshot(
        self, output_path: str
    ) -> bool:
        """Take a screenshot of a selected region using slurp + grim.

        Note: Requires user interaction (selection) so only useful when present.
        """
        if not self._display_available:
            return False

        try:
            # Get region from slurp
            slurp = await asyncio.create_subprocess_exec(
                "slurp",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            region_out, _ = await slurp.communicate()
            if slurp.returncode != 0:
                return False

            region = region_out.decode().strip()
            proc = await asyncio.create_subprocess_exec(
                "grim", "-g", region, output_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            return proc.returncode == 0
        except FileNotFoundError:
            return False

    # ------------------------------------------------------------------
    # Clipboard
    # ------------------------------------------------------------------

    async def get_clipboard(self) -> str | None:
        """Get clipboard contents using wl-paste."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "wl-paste",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode == 0:
                return stdout.decode()
            return None
        except FileNotFoundError:
            return None

    async def set_clipboard(self, text: str) -> bool:
        """Set clipboard contents using wl-copy."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "wl-copy",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate(input=text.encode())
            return proc.returncode == 0
        except FileNotFoundError:
            return False

    # ------------------------------------------------------------------
    # Headless fallback
    # ------------------------------------------------------------------

    async def run_in_tmux(
        self,
        session_name: str,
        command: str,
    ) -> bool:
        """Run a command in a tmux session (headless fallback).

        Args:
            session_name: tmux session name.
            command: Command to run inside tmux.

        Returns:
            True on success.
        """
        try:
            # Check if session exists
            check = await asyncio.create_subprocess_exec(
                "tmux", "has-session", "-t", session_name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await check.communicate()

            if check.returncode == 0:
                # Session exists, send command to it
                proc = await asyncio.create_subprocess_exec(
                    "tmux", "send-keys", "-t", session_name, command, "Enter",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            else:
                # Create new session
                proc = await asyncio.create_subprocess_exec(
                    "tmux", "new-session", "-d", "-s", session_name, command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )

            await proc.communicate()
            return proc.returncode == 0
        except FileNotFoundError:
            log.warning("tmux not found — install tmux for headless mode")
            return False

    async def capture_tmux(self, session_name: str) -> str | None:
        """Capture the visible contents of a tmux pane."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "tmux", "capture-pane", "-t", session_name, "-p",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode == 0:
                return stdout.decode()
            return None
        except FileNotFoundError:
            return None
