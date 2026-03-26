"""Tests for container manager."""

import json
from pathlib import Path

import pytest

from enclave.common.config import ContainerConfig
from enclave.orchestrator.container import ContainerManager, Session, _slugify


@pytest.fixture
def container_config(tmp_path: Path) -> ContainerConfig:
    return ContainerConfig(
        workspace_base=str(tmp_path / "workspaces"),
        session_base=str(tmp_path / "sessions"),
    )


@pytest.fixture
def manager(container_config: ContainerConfig) -> ContainerManager:
    return ContainerManager(container_config)


class TestSlugify:
    """Test name slugification."""

    def test_simple_name(self) -> None:
        assert _slugify("Hello World") == "hello-world"

    def test_underscores(self) -> None:
        assert _slugify("my_project") == "my-project"

    def test_dots(self) -> None:
        assert _slugify("my.project") == "my-project"

    def test_strips_dashes(self) -> None:
        assert _slugify("-leading-trailing-") == "leading-trailing"

    def test_truncates_long_names(self) -> None:
        long_name = "a" * 50
        assert len(_slugify(long_name)) <= 32

    def test_empty_string(self) -> None:
        assert _slugify("") == ""


class TestSessionCreation:
    """Test session creation and management."""

    async def test_create_session(self, manager: ContainerManager) -> None:
        session = await manager.create_session(
            name="Test Project",
            room_id="!abc:test.com",
            socket_path="/tmp/test.sock",
        )
        assert session.name == "Test Project"
        assert session.room_id == "!abc:test.com"
        assert session.status == "created"
        assert "test-project" in session.id
        assert Path(session.workspace_path).exists()

    async def test_get_session(self, manager: ContainerManager) -> None:
        session = await manager.create_session("Test", "!a:b", "/tmp/t.sock")
        retrieved = manager.get_session(session.id)
        assert retrieved is not None
        assert retrieved.id == session.id

    async def test_get_nonexistent_session(self, manager: ContainerManager) -> None:
        assert manager.get_session("nonexistent") is None

    async def test_get_session_by_room(self, manager: ContainerManager) -> None:
        session = await manager.create_session("Test", "!room:b", "/tmp/t.sock")
        session.status = "running"
        found = manager.get_session_by_room("!room:b")
        assert found is not None
        assert found.id == session.id

    async def test_get_session_by_room_not_running(self, manager: ContainerManager) -> None:
        await manager.create_session("Test", "!room:b", "/tmp/t.sock")
        # Status is "created", not "running"
        assert manager.get_session_by_room("!room:b") is None

    async def test_list_sessions(self, manager: ContainerManager) -> None:
        await manager.create_session("A", "!a:b", "/tmp/a.sock")
        await manager.create_session("B", "!b:b", "/tmp/b.sock")
        assert len(manager.list_sessions()) == 2

    async def test_active_sessions(self, manager: ContainerManager) -> None:
        s1 = await manager.create_session("A", "!a:b", "/tmp/a.sock")
        s2 = await manager.create_session("B", "!b:b", "/tmp/b.sock")
        s1.status = "running"
        active = manager.active_sessions()
        assert len(active) == 1
        assert active[0].id == s1.id


class TestSessionLifecycle:
    """Test session stop and removal."""

    async def test_remove_session(self, manager: ContainerManager) -> None:
        session = await manager.create_session("Test", "!a:b", "/tmp/t.sock")
        sid = session.id
        assert manager.get_session(sid) is not None
        await manager.remove_session(sid)
        assert manager.get_session(sid) is None

    async def test_remove_nonexistent(self, manager: ContainerManager) -> None:
        result = await manager.remove_session("nope")
        assert result is False

    async def test_workspace_created(
        self, manager: ContainerManager, container_config: ContainerConfig
    ) -> None:
        session = await manager.create_session("Workspace Test", "!a:b", "/tmp/t.sock")
        workspace = Path(container_config.workspace_base) / session.id
        assert workspace.exists()
        assert workspace.is_dir()

    async def test_session_state_dir_created(
        self, manager: ContainerManager, container_config: ContainerConfig
    ) -> None:
        session = await manager.create_session("State Test", "!a:b", "/tmp/t.sock")
        state = Path(container_config.session_base) / session.id
        assert state.exists()
        assert state.is_dir()


class TestSession:
    """Test Session dataclass."""

    def test_session_defaults(self) -> None:
        s = Session(id="test", name="Test", room_id="!a:b")
        assert s.status == "created"
        assert s.container_id is None
        assert s.created_at  # auto-generated

    def test_session_status_transitions(self) -> None:
        s = Session(id="test", name="Test", room_id="!a:b")
        assert s.status == "created"
        s.status = "starting"
        assert s.status == "starting"
        s.status = "running"
        assert s.status == "running"


class TestCheckHealth:
    """Test container health check."""

    @pytest.mark.asyncio
    async def test_no_running_sessions(self, manager: ContainerManager) -> None:
        crashed = await manager.check_health()
        assert crashed == []

    @pytest.mark.asyncio
    async def test_stopped_sessions_ignored(self, manager: ContainerManager) -> None:
        manager._sessions["s1"] = Session(
            id="s1", name="test", room_id="!r:t", status="stopped",
        )
        crashed = await manager.check_health()
        assert crashed == []

    @pytest.mark.asyncio
    async def test_marks_dead_container_as_stopped(
        self, manager: ContainerManager
    ) -> None:
        manager._sessions["s1"] = Session(
            id="s1", name="test", room_id="!r:t", status="running",
        )
        # get_container_status returns None for non-existent container
        crashed = await manager.check_health()
        assert len(crashed) == 1
        assert crashed[0].id == "s1"
        assert manager._sessions["s1"].status == "stopped"


class TestSessionPersistence:
    """Test session save/load with status preservation."""

    @pytest.fixture
    def manager_with_dir(self, tmp_path):
        """Container manager with writable session dir."""
        config = ContainerConfig()
        config.session_base = str(tmp_path / "sessions")
        config.workspace_base = str(tmp_path / "workspaces")
        Path(config.session_base).mkdir(parents=True)
        Path(config.workspace_base).mkdir(parents=True)
        return ContainerManager(config=config)

    @pytest.mark.asyncio
    async def test_save_preserves_status(self, manager_with_dir):
        """Running sessions save their status."""
        mgr = manager_with_dir
        s = await mgr.create_session("test", "!room:test", "/tmp/sock")
        s.status = "running"
        mgr._save_sessions()

        data = json.loads(Path(mgr._sessions_file).read_text())
        assert data[0]["status"] == "running"

    @pytest.mark.asyncio
    async def test_save_preserves_user_identity(self, manager_with_dir):
        """User display name and pronouns are persisted."""
        mgr = manager_with_dir
        s = await mgr.create_session(
            "test", "!room:test", "/tmp/sock",
            user_display_name="Ian",
            user_pronouns="he/him",
        )
        mgr._save_sessions()

        data = json.loads(Path(mgr._sessions_file).read_text())
        assert data[0]["user_display_name"] == "Ian"
        assert data[0]["user_pronouns"] == "he/him"

    @pytest.mark.asyncio
    async def test_load_running_becomes_was_running(self, manager_with_dir):
        """Saved 'running' sessions load as 'was_running'."""
        mgr = manager_with_dir
        s = await mgr.create_session("test", "!room:test", "/tmp/sock")
        s.status = "running"
        mgr._save_sessions()

        # Create new manager to reload
        mgr2 = ContainerManager(config=mgr.config)
        sessions = mgr2.list_sessions()
        assert len(sessions) == 1
        assert sessions[0].status == "was_running"

    @pytest.mark.asyncio
    async def test_load_stopped_stays_stopped(self, manager_with_dir):
        """Saved 'stopped' sessions load as 'stopped'."""
        mgr = manager_with_dir
        s = await mgr.create_session("test", "!room:test", "/tmp/sock")
        s.status = "stopped"
        mgr._save_sessions()

        mgr2 = ContainerManager(config=mgr.config)
        sessions = mgr2.list_sessions()
        assert len(sessions) == 1
        assert sessions[0].status == "stopped"

    @pytest.mark.asyncio
    async def test_sessions_needing_restore(self, manager_with_dir):
        """sessions_needing_restore returns only was_running sessions."""
        mgr = manager_with_dir
        s1 = await mgr.create_session("p1", "!r1:test", "/tmp/sock1")
        s1.status = "running"
        s2 = await mgr.create_session("p2", "!r2:test", "/tmp/sock2")
        s2.status = "stopped"
        mgr._save_sessions()

        mgr2 = ContainerManager(config=mgr.config)
        need_restore = mgr2.sessions_needing_restore()
        assert len(need_restore) == 1
        assert "p1" in need_restore[0].id

    @pytest.mark.asyncio
    async def test_load_preserves_user_identity(self, manager_with_dir):
        """User identity survives save/load cycle."""
        mgr = manager_with_dir
        s = await mgr.create_session(
            "test", "!room:test", "/tmp/sock",
            user_display_name="Ian",
            user_pronouns="he/him",
        )
        mgr._save_sessions()

        mgr2 = ContainerManager(config=mgr.config)
        loaded = mgr2.list_sessions()[0]
        assert loaded.user_display_name == "Ian"
        assert loaded.user_pronouns == "he/him"
