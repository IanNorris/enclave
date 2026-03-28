"""Desktop integration: Wayland session detection, GUI launching, screenshots.

Detects the active Wayland session and provides methods to:
- Launch GUI applications on the user's desktop
- Take screenshots and send them to Matrix
- Get/set clipboard contents
- Fall back to headless mode (tmux) when no display is available

Compositor support:
- Generic Wayland (any compositor): launch, screenshot, clipboard
- Hyprland: window listing, active window info
- Sway: window listing, active window info
- Others: graceful degradation to generic Wayland
"""

from __future__ import annotations

import asyncio
import os
import json
import shutil
from pathlib import Path

from enclave.common.logging import get_logger

log = get_logger("display")


class DisplayManager:
    """Manages desktop session detection and interaction.

    Works with any Wayland compositor. Compositor-specific features
    (window listing) degrade gracefully when unavailable.
    """

    def __init__(self, user: str = ""):
        self.user = user or os.environ.get("USER", "")
        self._display_available = False
        self._compositor: str = "generic"  # hyprland, sway, cosmic, generic
        self._wayland_display: str | None = None
        self._display_env: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Session detection
    # ------------------------------------------------------------------

    def detect_session(self) -> bool:
        """Detect active Wayland (or X11) session.

        Checks environment variables and compositor sockets.
        Returns True if a desktop session is available.
        """
        # Build display environment from current env or well-known paths
        self._display_env = {}
        xdg = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        self._display_env["XDG_RUNTIME_DIR"] = xdg

        # Check for Wayland display
        wayland = os.environ.get("WAYLAND_DISPLAY")
        if not wayland:
            # Scan XDG_RUNTIME_DIR for wayland sockets
            xdg_path = Path(xdg)
            if xdg_path.exists():
                for sock in sorted(xdg_path.glob("wayland-*")):
                    if sock.is_socket():
                        wayland = sock.name
                        break

        if wayland:
            self._wayland_display = wayland
            self._display_env["WAYLAND_DISPLAY"] = wayland
            self._display_available = True

            # Detect compositor type
            self._compositor = self._detect_compositor()
            log.info(
                "Wayland session detected: %s (compositor: %s)",
                wayland,
                self._compositor,
            )

            # Pass through X11 display for XWayland apps
            x_display = os.environ.get("DISPLAY")
            if x_display:
                self._display_env["DISPLAY"] = x_display

            return True

        # X11 fallback
        x_display = os.environ.get("DISPLAY")
        if x_display:
            self._display_env["DISPLAY"] = x_display
            self._display_available = True
            self._compositor = "x11"
            log.info("X11 session detected: %s", x_display)
            return True

        self._display_available = False
        log.info("No desktop session detected")
        return False

    def _detect_compositor(self) -> str:
        """Detect which Wayland compositor is running."""
        # Check environment hints
        desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
        if "hyprland" in desktop:
            return "hyprland"
        if "sway" in desktop:
            return "sway"
        if "cosmic" in desktop:
            return "cosmic"

        hypr_sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
        if hypr_sig:
            return "hyprland"
        if os.environ.get("SWAYSOCK"):
            return "sway"

        # Check for running compositor binaries
        if shutil.which("hyprctl") and self._check_hyprland_running():
            return "hyprland"
        if shutil.which("swaymsg"):
            return "sway"

        return "generic"

    def _check_hyprland_running(self) -> bool:
        """Check if Hyprland sockets exist."""
        for base in [Path("/tmp/hypr"), Path(self._display_env.get("XDG_RUNTIME_DIR", "")) / "hypr"]:
            if base.exists():
                for d in base.iterdir():
                    if (d / ".socket.sock").exists():
                        return True
        return False

    @property
    def is_available(self) -> bool:
        """Whether a desktop session is available."""
        return self._display_available

    @property
    def session_type(self) -> str:
        """Type of display session (compositor name or 'headless')."""
        if self._display_available:
            return self._compositor
        return "headless"

    def get_display_env(self) -> dict[str, str]:
        """Get environment variables needed for GUI apps."""
        return dict(self._display_env)

    # ------------------------------------------------------------------
    # Compositor interaction
    # ------------------------------------------------------------------

    async def _run_cmd(
        self, *args: str, env_override: dict[str, str] | None = None,
    ) -> tuple[int, str, str]:
        """Run a command with display environment, return (rc, stdout, stderr)."""
        env = dict(os.environ)
        env.update(self._display_env)
        if env_override:
            env.update(env_override)

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout, stderr = await proc.communicate()
            return proc.returncode, stdout.decode().strip(), stderr.decode().strip()
        except FileNotFoundError:
            return -1, "", f"{args[0]} not found"

    async def launch_app(self, command: str, extra_env: dict[str, str] | None = None) -> bool:
        """Launch a GUI application on the desktop.

        Uses compositor-native launching when available, otherwise
        spawns the process directly with the correct display env.

        Args:
            command: The command to run (e.g., "firefox", "code .")
            extra_env: Additional environment variables to set.

        Returns:
            True if launched successfully.
        """
        if not self._display_available:
            log.info("No desktop available, cannot launch GUI: %s", command)
            return False

        # Compositor-native launch (better window management)
        if self._compositor == "hyprland":
            rc, _, err = await self._run_cmd("hyprctl", "dispatch", "exec", command)
            if rc == 0:
                log.info("Launched via hyprctl: %s", command)
                return True
            log.warning("hyprctl launch failed: %s", err)

        elif self._compositor == "sway":
            rc, _, err = await self._run_cmd("swaymsg", "exec", command)
            if rc == 0:
                log.info("Launched via swaymsg: %s", command)
                return True
            log.warning("swaymsg launch failed: %s", err)

        # Generic fallback — spawn with display env
        env = dict(os.environ)
        env.update(self._display_env)
        if extra_env:
            env.update(extra_env)
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                start_new_session=True,
            )
            log.info("Launched GUI app (generic): %s (pid=%d)", command, proc.pid)

            # Monitor stderr briefly for early failures
            async def _watch_stderr() -> None:
                try:
                    data = await asyncio.wait_for(proc.stderr.read(4096), timeout=5.0)
                    if data:
                        log.warning("GUI app stderr: %s", data.decode(errors="replace").strip())
                except asyncio.TimeoutError:
                    pass  # Still running, no early error
                except Exception:
                    pass
            asyncio.create_task(_watch_stderr())

            return True
        except Exception as e:
            log.error("Failed to launch GUI app: %s", e)
            return False

    async def get_active_window(self) -> dict | None:
        """Get info about the currently active window.

        Returns a dict with at least 'title' and 'class' keys, or None.
        """
        if self._compositor == "hyprland":
            rc, out, _ = await self._run_cmd("hyprctl", "activewindow", "-j")
            if rc == 0 and out:
                try:
                    return json.loads(out)
                except json.JSONDecodeError:
                    pass

        elif self._compositor == "sway":
            rc, out, _ = await self._run_cmd(
                "swaymsg", "-t", "get_tree",
            )
            if rc == 0 and out:
                try:
                    tree = json.loads(out)
                    return self._sway_find_focused(tree)
                except json.JSONDecodeError:
                    pass

        return None

    def _sway_find_focused(self, node: dict) -> dict | None:
        """Recursively find the focused node in a sway tree."""
        if node.get("focused"):
            return {
                "title": node.get("name", ""),
                "class": node.get("app_id", ""),
                "pid": node.get("pid"),
            }
        for child in node.get("nodes", []) + node.get("floating_nodes", []):
            result = self._sway_find_focused(child)
            if result:
                return result
        return None

    async def list_windows(self) -> list[dict]:
        """List all windows."""
        if self._compositor == "hyprland":
            rc, out, _ = await self._run_cmd("hyprctl", "clients", "-j")
            if rc == 0 and out:
                try:
                    return json.loads(out)
                except json.JSONDecodeError:
                    pass

        elif self._compositor == "sway":
            rc, out, _ = await self._run_cmd("swaymsg", "-t", "get_tree")
            if rc == 0 and out:
                try:
                    tree = json.loads(out)
                    return self._sway_collect_windows(tree)
                except json.JSONDecodeError:
                    pass

        return []

    def _sway_collect_windows(self, node: dict) -> list[dict]:
        """Collect all leaf windows from a sway tree."""
        windows = []
        if node.get("type") == "con" and node.get("name"):
            windows.append({
                "title": node.get("name", ""),
                "class": node.get("app_id", ""),
                "pid": node.get("pid"),
                "focused": node.get("focused", False),
            })
        for child in node.get("nodes", []) + node.get("floating_nodes", []):
            windows.extend(self._sway_collect_windows(child))
        return windows

    # ------------------------------------------------------------------
    # Screenshots
    # ------------------------------------------------------------------

    async def take_screenshot(self, output_path: str) -> bool:
        """Take a screenshot using grim (Wayland) or scrot (X11).

        Args:
            output_path: Where to save the screenshot (PNG).

        Returns:
            True on success.
        """
        if not self._display_available:
            log.info("No desktop available for screenshot")
            return False

        if self._compositor != "x11":
            # Wayland: use grim
            rc, _, err = await self._run_cmd("grim", output_path)
            if rc == 0:
                log.info("Screenshot saved: %s", output_path)
                return True
            log.warning("grim failed: %s", err)
        else:
            # X11: use scrot
            rc, _, err = await self._run_cmd("scrot", output_path)
            if rc == 0:
                log.info("Screenshot saved: %s", output_path)
                return True
            log.warning("scrot failed: %s", err)

        return False

    async def take_region_screenshot(self, output_path: str) -> bool:
        """Take a screenshot of a selected region using slurp + grim.

        Note: Requires user interaction (selection).
        """
        if not self._display_available:
            return False

        if self._compositor == "x11":
            rc, _, _ = await self._run_cmd("scrot", "-s", output_path)
            return rc == 0

        # Wayland: slurp for region, grim to capture
        rc, region, _ = await self._run_cmd("slurp")
        if rc != 0:
            return False

        rc, _, _ = await self._run_cmd("grim", "-g", region, output_path)
        return rc == 0

    # ------------------------------------------------------------------
    # Clipboard
    # ------------------------------------------------------------------

    async def get_clipboard(self) -> str | None:
        """Get clipboard contents (wl-paste for Wayland, xclip for X11)."""
        if self._compositor != "x11":
            rc, out, _ = await self._run_cmd("wl-paste")
        else:
            rc, out, _ = await self._run_cmd("xclip", "-selection", "clipboard", "-o")
        return out if rc == 0 else None

    async def set_clipboard(self, text: str) -> bool:
        """Set clipboard contents (wl-copy for Wayland, xclip for X11)."""
        env = dict(os.environ)
        env.update(self._display_env)

        try:
            if self._compositor != "x11":
                cmd = ["wl-copy"]
            else:
                cmd = ["xclip", "-selection", "clipboard"]

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
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
                proc = await asyncio.create_subprocess_exec(
                    "tmux", "send-keys", "-t", session_name, command, "Enter",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            else:
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
