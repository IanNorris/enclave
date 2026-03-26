"""Tests for host-mode agent execution."""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from enclave.common.config import ContainerConfig, ContainerProfile
from enclave.orchestrator.container import ContainerManager, Session


def _make_config(tmp_path: Path) -> ContainerConfig:
    """Create a ContainerConfig for testing."""
    ws = tmp_path / "workspaces"
    ws.mkdir()
    sess = tmp_path / "sessions"
    sess.mkdir()
    return ContainerConfig(
        image="enclave-agent:latest",
        runtime="podman",
        workspace_base=str(ws),
        session_base=str(sess),
        profiles={
            "dev": ContainerProfile(image="enclave-agent:latest"),
            "host": ContainerProfile(image="", description="Host mode"),
        },
        default_profile="dev",
    )


class TestHostModeDetection:
    """Test that host mode is correctly detected from profile."""

    def test_host_profile_has_empty_image(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        profile = config.get_profile("host")
        assert profile.image == ""

    def test_dev_profile_has_image(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        profile = config.get_profile("dev")
        assert profile.image != ""


class TestHostModeStart:
    """Test starting agents in host mode."""

    @pytest.mark.asyncio
    async def test_start_host_session_success(self, tmp_path: Path) -> None:
        """Host mode should spawn a subprocess instead of a container."""
        config = _make_config(tmp_path)
        mgr = ContainerManager(config)

        ws = tmp_path / "workspaces" / "test-ws"
        ws.mkdir(parents=True)
        sock_dir = tmp_path / "sockets"
        sock_dir.mkdir()
        sock_path = sock_dir / "test.sock"

        session = await mgr.create_session(
            name="test-host",
            room_id="!test:matrix.local",
            socket_path=str(sock_path),
            profile="host",
        )

        # Mock asyncio.create_subprocess_exec to avoid actually spawning
        mock_proc = AsyncMock()
        mock_proc.pid = 12345
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            success, error = await mgr.start_session(session.id)

        assert success
        assert error == ""
        assert session.host_pid == 12345
        assert session.status == "running"

    @pytest.mark.asyncio
    async def test_start_host_session_sets_env(self, tmp_path: Path) -> None:
        """Host mode passes correct environment variables."""
        config = _make_config(tmp_path)
        config.github_token = "test-token"
        mgr = ContainerManager(config)

        ws = tmp_path / "workspaces" / "test-ws"
        ws.mkdir(parents=True)
        sock_dir = tmp_path / "sockets"
        sock_dir.mkdir()
        sock_path = sock_dir / "test.sock"

        session = await mgr.create_session(
            name="test-host",
            room_id="!test:matrix.local",
            socket_path=str(sock_path),
            profile="host",
            user_display_name="Ian",
            user_pronouns="he/him",
        )

        captured_env = {}
        mock_proc = AsyncMock()
        mock_proc.pid = 12345
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.returncode = 0

        async def capture_exec(*args, **kwargs):
            captured_env.update(kwargs.get("env", {}))
            return mock_proc

        with patch("asyncio.create_subprocess_exec", side_effect=capture_exec):
            await mgr.start_session(session.id)

        assert captured_env["SESSION_ID"] == session.id
        assert captured_env["SESSION_NAME"] == "test-host"
        assert captured_env["GITHUB_TOKEN"] == "test-token"
        assert captured_env["ENCLAVE_HOST_MODE"] == "1"
        assert captured_env["ENCLAVE_USER_NAME"] == "Ian"
        assert captured_env["ENCLAVE_USER_PRONOUNS"] == "he/him"
        assert captured_env["ENCLAVE_WORKSPACE"] == session.workspace_path


class TestHostModeStop:
    """Test stopping host-mode agents."""

    @pytest.mark.asyncio
    async def test_stop_host_session_sends_sigterm(self, tmp_path: Path) -> None:
        """Stopping a host session should send SIGTERM."""
        config = _make_config(tmp_path)
        mgr = ContainerManager(config)

        ws = tmp_path / "workspaces" / "test-ws"
        ws.mkdir(parents=True)

        session = await mgr.create_session(
            name="test-host",
            room_id="!test:matrix.local",
            socket_path=str(tmp_path / "test.sock"),
            profile="host",
        )
        session.status = "running"
        session.host_pid = 99999

        with patch("os.kill") as mock_kill:
            # First call SIGTERM succeeds, second call (check) raises ProcessLookupError
            mock_kill.side_effect = [None, ProcessLookupError]
            result = await mgr.stop_session(session.id)

        assert result
        assert session.status == "stopped"
        assert session.host_pid is None
        mock_kill.assert_any_call(99999, signal.SIGTERM)

    @pytest.mark.asyncio
    async def test_stop_host_already_dead(self, tmp_path: Path) -> None:
        """If process already exited, stop should still succeed."""
        config = _make_config(tmp_path)
        mgr = ContainerManager(config)

        ws = tmp_path / "workspaces" / "test-ws"
        ws.mkdir(parents=True)

        session = await mgr.create_session(
            name="test-host",
            room_id="!test:matrix.local",
            socket_path=str(tmp_path / "test.sock"),
            profile="host",
        )
        session.status = "running"
        session.host_pid = 99999

        with patch("os.kill", side_effect=ProcessLookupError):
            result = await mgr.stop_session(session.id)

        assert result
        assert session.status == "stopped"


class TestHostModeHealthCheck:
    """Test health checks for host-mode agents."""

    @pytest.mark.asyncio
    async def test_health_check_host_alive(self, tmp_path: Path) -> None:
        """Running host process should pass health check."""
        config = _make_config(tmp_path)
        mgr = ContainerManager(config)

        ws = tmp_path / "workspaces" / "test-ws"
        ws.mkdir(parents=True)

        session = await mgr.create_session(
            name="test-host",
            room_id="!test:matrix.local",
            socket_path=str(tmp_path / "test.sock"),
            profile="host",
        )
        session.status = "running"
        session.host_pid = os.getpid()  # Use own PID (always alive)

        with patch("os.kill", return_value=None):  # os.kill(pid, 0) succeeds
            crashed = await mgr.check_health()

        assert len(crashed) == 0
        assert session.status == "running"

    @pytest.mark.asyncio
    async def test_health_check_host_dead(self, tmp_path: Path) -> None:
        """Dead host process should be marked crashed."""
        config = _make_config(tmp_path)
        mgr = ContainerManager(config)

        ws = tmp_path / "workspaces" / "test-ws"
        ws.mkdir(parents=True)

        session = await mgr.create_session(
            name="test-host",
            room_id="!test:matrix.local",
            socket_path=str(tmp_path / "test.sock"),
            profile="host",
        )
        session.status = "running"
        session.host_pid = 99999

        with patch("os.kill", side_effect=ProcessLookupError):
            crashed = await mgr.check_health()

        assert len(crashed) == 1
        assert crashed[0].id == session.id
        assert session.status == "stopped"
        assert session.host_pid is None
