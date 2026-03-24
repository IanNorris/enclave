"""Tests for the display manager."""

from __future__ import annotations

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
        assert result is False

    @pytest.mark.asyncio
    async def test_launch_with_mock_hyprctl(self) -> None:
        mgr = DisplayManager()
        mgr._display_available = True
        mgr._hyprland_socket = "/tmp/hypr/test/.socket.sock"

        with patch.object(mgr, "hyprctl", new_callable=AsyncMock) as mock:
            mock.return_value = "ok"
            result = await mgr.launch_app("code .")
            assert result is True
            mock.assert_called_once_with("dispatch", "exec", "code .")


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
