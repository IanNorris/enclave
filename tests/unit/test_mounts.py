"""Tests for the mount manager.

Uses mocked commands since real mounts require root/sudo.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from enclave.orchestrator.mounts import MountManager, MountPoint


@pytest.fixture
def mount_mgr():
    """Create a mount manager with sudo disabled."""
    return MountManager(use_sudo=False)


class TestSanitizeName:
    def test_simple_path(self, mount_mgr: MountManager) -> None:
        assert mount_mgr._sanitize_name("/home/user/code") == "home-user-code"

    def test_strips_leading_slash(self, mount_mgr: MountManager) -> None:
        result = mount_mgr._sanitize_name("/tmp/foo")
        assert not result.startswith("/")

    def test_replaces_spaces(self, mount_mgr: MountManager) -> None:
        assert mount_mgr._sanitize_name("/my path/here") == "my-path-here"

    def test_removes_double_dots(self, mount_mgr: MountManager) -> None:
        result = mount_mgr._sanitize_name("/home/../etc/passwd")
        assert ".." not in result

    def test_truncates_long_names(self, mount_mgr: MountManager) -> None:
        long_path = "/a" * 100
        result = mount_mgr._sanitize_name(long_path)
        assert len(result) <= 64


class TestWorkspaceTarget:
    def test_creates_target_path(self, mount_mgr: MountManager) -> None:
        result = mount_mgr._workspace_target("/workspace", "my-mount")
        assert result == "/workspace/my-mount"


class TestGrantMount:
    @pytest.mark.asyncio
    async def test_nonexistent_source_fails(self, mount_mgr: MountManager) -> None:
        result = await mount_mgr.grant_mount(
            "s1", "/workspace", "/nonexistent/path/that/doesnt/exist"
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_successful_mount(self, mount_mgr: MountManager) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            source = Path(tmpdir) / "source"
            source.mkdir()

            with patch("enclave.orchestrator.mounts._run", new_callable=AsyncMock) as mock_run:
                mock_run.return_value = 0
                result = await mount_mgr.grant_mount(
                    "s1", str(workspace), str(source)
                )

                assert result is not None
                assert result.source == str(source)
                assert result.session_id == "s1"
                assert result.active is True
                mock_run.assert_called_once()

    @pytest.mark.asyncio
    async def test_custom_mount_name(self, mount_mgr: MountManager) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            source = Path(tmpdir) / "source"
            source.mkdir()

            with patch("enclave.orchestrator.mounts._run", new_callable=AsyncMock) as mock_run:
                mock_run.return_value = 0
                result = await mount_mgr.grant_mount(
                    "s1", str(workspace), str(source), mount_name="my-code"
                )

                assert result is not None
                assert result.mount_name == "my-code"

    @pytest.mark.asyncio
    async def test_mount_failure(self, mount_mgr: MountManager) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            source = Path(tmpdir) / "source"
            source.mkdir()

            with patch("enclave.orchestrator.mounts._run", new_callable=AsyncMock) as mock_run:
                mock_run.return_value = 1  # Failure
                result = await mount_mgr.grant_mount(
                    "s1", str(workspace), str(source)
                )
                assert result is None


class TestRevokeMount:
    @pytest.mark.asyncio
    async def test_revoke_mount(self, mount_mgr: MountManager) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            source = Path(tmpdir) / "source"
            source.mkdir()

            with patch("enclave.orchestrator.mounts._run", new_callable=AsyncMock) as mock_run:
                mock_run.return_value = 0
                await mount_mgr.grant_mount("s1", str(workspace), str(source))
                mount = mount_mgr.list_mounts("s1")[0]

                ok = await mount_mgr.revoke_mount("s1", str(workspace), mount.mount_name)
                assert ok is True
                assert len(mount_mgr.list_mounts("s1")) == 0

    @pytest.mark.asyncio
    async def test_revoke_all(self, mount_mgr: MountManager) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            s1 = Path(tmpdir) / "s1"
            s2 = Path(tmpdir) / "s2"
            s1.mkdir()
            s2.mkdir()

            with patch("enclave.orchestrator.mounts._run", new_callable=AsyncMock) as mock_run:
                mock_run.return_value = 0
                await mount_mgr.grant_mount("sess", str(workspace), str(s1))
                await mount_mgr.grant_mount("sess", str(workspace), str(s2))

                count = await mount_mgr.revoke_all("sess", str(workspace))
                assert count == 2
                assert len(mount_mgr.list_mounts("sess")) == 0


class TestListAndCheck:
    @pytest.mark.asyncio
    async def test_list_mounts(self, mount_mgr: MountManager) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            source = Path(tmpdir) / "source"
            source.mkdir()

            with patch("enclave.orchestrator.mounts._run", new_callable=AsyncMock) as mock_run:
                mock_run.return_value = 0
                await mount_mgr.grant_mount("s1", str(workspace), str(source))

                mounts = mount_mgr.list_mounts("s1")
                assert len(mounts) == 1
                assert mounts[0].source == str(source)

    def test_list_empty_session(self, mount_mgr: MountManager) -> None:
        assert mount_mgr.list_mounts("nonexistent") == []

    @pytest.mark.asyncio
    async def test_has_mount(self, mount_mgr: MountManager) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            source = Path(tmpdir) / "source"
            source.mkdir()

            with patch("enclave.orchestrator.mounts._run", new_callable=AsyncMock) as mock_run:
                mock_run.return_value = 0
                await mount_mgr.grant_mount("s1", str(workspace), str(source))

                assert mount_mgr.has_mount("s1", str(source)) is True
                assert mount_mgr.has_mount("s1", "/other") is False


class TestSharedPropagation:
    @pytest.mark.asyncio
    async def test_setup_shared_propagation(self, mount_mgr: MountManager) -> None:
        with patch("enclave.orchestrator.mounts._run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = 0
            ok = await mount_mgr.setup_shared_propagation("/workspace")
            assert ok is True
            assert mock_run.call_count == 2

    @pytest.mark.asyncio
    async def test_setup_failure(self, mount_mgr: MountManager) -> None:
        with patch("enclave.orchestrator.mounts._run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = 1
            ok = await mount_mgr.setup_shared_propagation("/workspace")
            assert ok is False
