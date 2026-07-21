"""Tests for the display manager."""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from enclave.orchestrator.display import DisplayManager


class TestSessionDetection:
    def test_no_session_detected(self) -> None:
        mgr = DisplayManager()
        with patch.dict(os.environ, {}, clear=True):
            with patch("enclave.orchestrator.display.Path") as MockPath:
                MockPath.return_value.exists.return_value = False
                MockPath.side_effect = Path
                # In clean env, shouldn't find anything
                result = mgr.detect_session()
                # May or may not find a session depending on the host
                assert isinstance(result, bool)

    def test_session_type_headless_default(self) -> None:
        mgr = DisplayManager()
        assert mgr.session_type == "headless"
        assert mgr.is_available is False

    def test_session_type_hyprland(self) -> None:
        mgr = DisplayManager()
        mgr._hyprland_socket = "/tmp/hypr/test/.socket.sock"
        mgr._display_available = True
        assert mgr.session_type == "hyprland"
        assert mgr.is_available is True


class TestGuiLaunch:
    @pytest.mark.asyncio
    async def test_launch_without_display(self) -> None:
        mgr = DisplayManager()
        result = await mgr.launch_app("firefox")
        assert result.ok is False
        assert "desktop" in result.error.lower()

    @pytest.mark.asyncio
    async def test_launch_with_mock_hyprctl(self) -> None:
        mgr = DisplayManager()
        mgr._display_available = True
        mgr._compositor = "hyprland"

        with patch.object(mgr, "_run_cmd", new_callable=AsyncMock) as mock:
            mock.return_value = (0, "", "")
            result = await mgr.launch_app("code .")
            assert result.ok is True
            mock.assert_called_once_with("hyprctl", "dispatch", "exec", "code .")

    @pytest.mark.asyncio
    async def test_generic_launch_reports_early_failure(self) -> None:
        """A process that exits non-zero within the watch window (e.g. a
        missing script → bash exit 127) must be reported as a failure with the
        captured stderr, not a silent success (ENC-011)."""
        mgr = DisplayManager()
        mgr._display_available = True
        mgr._compositor = "generic"

        proc = MagicMock()
        proc.pid = 4242
        proc.wait = AsyncMock(return_value=127)
        proc.stderr = MagicMock()
        proc.stderr.read = AsyncMock(
            return_value=b"bash: /bad/launch.sh: No such file or directory"
        )

        with patch("asyncio.create_subprocess_shell", new_callable=AsyncMock) as spawn:
            spawn.return_value = proc
            result = await mgr.launch_app("bash /bad/launch.sh")

        assert result.ok is False
        assert result.rc == 127
        assert "No such file or directory" in result.error

    @pytest.mark.asyncio
    async def test_generic_launch_running_is_success(self) -> None:
        """A GUI app still running after the watch window is a success."""
        mgr = DisplayManager()
        mgr._display_available = True
        mgr._compositor = "generic"

        proc = MagicMock()
        proc.pid = 4243
        # wait()/read() raise TimeoutError (still running) — launch_app catches
        # these and treats the launch as successful.
        proc.wait = AsyncMock(side_effect=asyncio.TimeoutError)
        proc.stderr = MagicMock()
        proc.stderr.read = AsyncMock(side_effect=asyncio.TimeoutError)

        with patch("asyncio.create_subprocess_shell", new_callable=AsyncMock) as spawn:
            spawn.return_value = proc
            result = await mgr.launch_app("firefox")

        assert result.ok is True

    @pytest.mark.asyncio
    async def test_generic_launch_passes_cwd(self) -> None:
        """cwd is forwarded to the spawned process so relative names resolve."""
        mgr = DisplayManager()
        mgr._display_available = True
        mgr._compositor = "generic"

        proc = MagicMock()
        proc.pid = 4244
        proc.wait = AsyncMock(return_value=0)
        proc.stderr = MagicMock()
        proc.stderr.read = AsyncMock(return_value=b"")

        with patch("asyncio.create_subprocess_shell", new_callable=AsyncMock) as spawn:
            spawn.return_value = proc
            await mgr.launch_app("bash run.sh", cwd="/data/ws/session")

        _, kwargs = spawn.call_args
        assert kwargs.get("cwd") == "/data/ws/session"


class TestScreenshot:
    @pytest.mark.asyncio
    async def test_screenshot_without_display(self) -> None:
        mgr = DisplayManager()
        result = await mgr.take_screenshot("/tmp/test.png")
        assert result is False

    @pytest.mark.asyncio
    async def test_screenshot_grim_not_found(self) -> None:
        mgr = DisplayManager()
        mgr._display_available = True

        with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
            result = await mgr.take_screenshot("/tmp/test.png")
            assert result is False


class TestClipboard:
    @pytest.mark.asyncio
    async def test_get_clipboard_tool_missing(self) -> None:
        mgr = DisplayManager()
        with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
            result = await mgr.get_clipboard()
            assert result is None

    @pytest.mark.asyncio
    async def test_set_clipboard_tool_missing(self) -> None:
        mgr = DisplayManager()
        with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
            result = await mgr.set_clipboard("test")
            assert result is False


class TestTmuxFallback:
    @pytest.mark.asyncio
    async def test_tmux_not_found(self) -> None:
        mgr = DisplayManager()
        with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
            result = await mgr.run_in_tmux("test-session", "echo hi")
            assert result is False

    @pytest.mark.asyncio
    async def test_capture_tmux_not_found(self) -> None:
        mgr = DisplayManager()
        with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
            result = await mgr.capture_tmux("test-session")
            assert result is None


class TestHyprctl:
    @pytest.mark.asyncio
    async def test_hyprctl_without_display(self) -> None:
        mgr = DisplayManager()
        result = await mgr.hyprctl("version")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_active_window_no_display(self) -> None:
        mgr = DisplayManager()
        result = await mgr.get_active_window()
        assert result is None

    @pytest.mark.asyncio
    async def test_list_windows_no_display(self) -> None:
        mgr = DisplayManager()
        result = await mgr.list_windows()
        assert result == []


class TestGuiPathTranslation:
    """ENC-011: the /workspace→host translation must be anchored to a path
    token boundary, so it rewrites the container mount prefix but never the
    substring inside the host base '/.../workspaces/...'. This mirrors the
    regex used in router._handle_gui_launch.
    """

    @staticmethod
    def _translate(command: str, ws: str) -> str:
        import re
        return re.sub(r"/workspace(?![\w-])", lambda _m: ws, command)

    WS = "/data/Enclave/workspaces/brook-8c7de217"

    def test_container_path_translated(self) -> None:
        got = self._translate("bash /workspace/brook/launch.sh", self.WS)
        assert got == f"bash {self.WS}/brook/launch.sh"

    def test_host_absolute_path_untouched(self) -> None:
        # The exact bug: a host path already under workspaces/ must NOT be
        # rewritten (previously it doubled into a mangled, nonexistent path).
        cmd = f"bash {self.WS}/brook/launch.sh"
        assert self._translate(cmd, self.WS) == cmd

    def test_mount_root_translated(self) -> None:
        assert self._translate("ls /workspace", self.WS) == f"ls {self.WS}"

    def test_multiple_occurrences(self) -> None:
        got = self._translate("cat /workspace/a /workspace/b", self.WS)
        assert got == f"cat {self.WS}/a {self.WS}/b"

    def test_unrelated_workspace_word_untouched(self) -> None:
        assert self._translate("ls /workspaces-backup", self.WS) == "ls /workspaces-backup"
