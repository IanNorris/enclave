"""Tests for the permission database."""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

import pytest

from enclave.orchestrator.permissions import (
    AuditEntry,
    PermissionDB,
    PermissionGrant,
    PermissionRequest,
    PermissionScope,
    PermissionType,
    RequestStatus,
)


@pytest.fixture
def db():
    """Create a temporary permission database."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_permissions.db"
        pdb = PermissionDB(db_path)
        yield pdb
        pdb.close()


# ------------------------------------------------------------------
# Grant tests
# ------------------------------------------------------------------


class TestGrants:
    """Test permission grant operations."""

    def test_add_and_get_grant(self, db: PermissionDB) -> None:
        grant_id = db.add_grant(
            session_id="s1",
            project_name="myapp",
            perm_type=PermissionType.FILESYSTEM,
            target="/home/user/projects/myapp",
            scope=PermissionScope.SESSION,
            granted_by="@ian:test",
        )
        grant = db.get_grant(grant_id)

        assert grant is not None
        assert grant.session_id == "s1"
        assert grant.project_name == "myapp"
        assert grant.perm_type == PermissionType.FILESYSTEM
        assert grant.target == "/home/user/projects/myapp"
        assert grant.scope == PermissionScope.SESSION
        assert grant.granted_by == "@ian:test"
        assert grant.is_active is True

    def test_grant_once_scope(self, db: PermissionDB) -> None:
        grant_id = db.add_grant(
            session_id="s1",
            project_name="myapp",
            perm_type=PermissionType.FILESYSTEM,
            target="/tmp/file.txt",
            scope=PermissionScope.ONCE,
            granted_by="@ian:test",
        )
        grant = db.get_grant(grant_id)
        assert grant is not None
        assert grant.is_active is True

        db.use_grant(grant_id)
        grant = db.get_grant(grant_id)
        assert grant is not None
        assert grant.used_count == 1
        assert grant.is_active is False

    def test_grant_with_expiry(self, db: PermissionDB) -> None:
        grant_id = db.add_grant(
            session_id="s1",
            project_name="myapp",
            perm_type=PermissionType.NETWORK,
            target="api.github.com",
            scope=PermissionScope.SESSION,
            granted_by="@ian:test",
            expires_at=time.time() - 10,  # Already expired
        )
        grant = db.get_grant(grant_id)
        assert grant is not None
        assert grant.is_active is False

    def test_grant_with_pattern(self, db: PermissionDB) -> None:
        grant_id = db.add_grant(
            session_id="s1",
            project_name="myapp",
            perm_type=PermissionType.FILESYSTEM,
            target="/home/user/projects/*",
            scope=PermissionScope.PATTERN,
            granted_by="@ian:test",
            pattern=r"/home/user/projects/.*",
        )
        grant = db.get_grant(grant_id)
        assert grant is not None
        assert grant.pattern == r"/home/user/projects/.*"

    def test_revoke_grant(self, db: PermissionDB) -> None:
        grant_id = db.add_grant(
            session_id="s1",
            project_name="myapp",
            perm_type=PermissionType.FILESYSTEM,
            target="/tmp",
            scope=PermissionScope.SESSION,
            granted_by="@ian:test",
        )
        assert db.revoke_grant(grant_id, "@ian:test") is True
        grant = db.get_grant(grant_id)
        assert grant is not None
        assert grant.is_active is False
        assert grant.revoked is True

    def test_revoke_nonexistent(self, db: PermissionDB) -> None:
        assert db.revoke_grant(99999, "@ian:test") is False

    def test_revoke_already_revoked(self, db: PermissionDB) -> None:
        grant_id = db.add_grant(
            session_id="s1",
            project_name="myapp",
            perm_type=PermissionType.FILESYSTEM,
            target="/tmp",
            scope=PermissionScope.SESSION,
            granted_by="@ian:test",
        )
        db.revoke_grant(grant_id, "@ian:test")
        assert db.revoke_grant(grant_id, "@ian:test") is False

    def test_list_grants(self, db: PermissionDB) -> None:
        db.add_grant("s1", "myapp", PermissionType.FILESYSTEM, "/a", PermissionScope.SESSION, "@ian:test")
        db.add_grant("s1", "myapp", PermissionType.FILESYSTEM, "/b", PermissionScope.SESSION, "@ian:test")
        db.add_grant("s2", "other", PermissionType.NETWORK, "x.com", PermissionScope.PROJECT, "@ian:test")

        all_grants = db.list_grants()
        assert len(all_grants) == 3

        s1_grants = db.list_grants(session_id="s1")
        assert len(s1_grants) == 2

        project_grants = db.list_grants(project_name="other")
        assert len(project_grants) == 1

    def test_list_grants_active_only(self, db: PermissionDB) -> None:
        g1 = db.add_grant("s1", "p", PermissionType.FILESYSTEM, "/a", PermissionScope.SESSION, "@u:t")
        g2 = db.add_grant("s1", "p", PermissionType.FILESYSTEM, "/b", PermissionScope.SESSION, "@u:t")
        db.revoke_grant(g1, "@u:t")

        active = db.list_grants(active_only=True)
        assert len(active) == 1

        all_grants = db.list_grants(active_only=False)
        assert len(all_grants) == 2

    def test_revoke_session_grants(self, db: PermissionDB) -> None:
        db.add_grant("s1", "p", PermissionType.FILESYSTEM, "/a", PermissionScope.SESSION, "@u:t")
        db.add_grant("s1", "p", PermissionType.FILESYSTEM, "/b", PermissionScope.SESSION, "@u:t")
        db.add_grant("s1", "p", PermissionType.FILESYSTEM, "/c", PermissionScope.PROJECT, "@u:t")

        count = db.revoke_session_grants("s1", "@u:t")
        assert count == 2

        active = db.list_grants(session_id="s1", active_only=True)
        # Only the project-scope grant remains
        assert len(active) == 1
        assert active[0].scope == PermissionScope.PROJECT


# ------------------------------------------------------------------
# Check permission tests
# ------------------------------------------------------------------


class TestCheckPermission:
    """Test permission checking logic."""

    def test_check_session_grant(self, db: PermissionDB) -> None:
        db.add_grant("s1", "p", PermissionType.FILESYSTEM, "/tmp", PermissionScope.SESSION, "@u:t")
        result = db.check_permission("s1", "p", PermissionType.FILESYSTEM, "/tmp")
        assert result is not None
        assert result.target == "/tmp"

    def test_check_project_grant(self, db: PermissionDB) -> None:
        db.add_grant("s1", "myapp", PermissionType.FILESYSTEM, "/src", PermissionScope.PROJECT, "@u:t")
        # Different session, same project
        result = db.check_permission("s2", "myapp", PermissionType.FILESYSTEM, "/src")
        assert result is not None

    def test_check_once_grant(self, db: PermissionDB) -> None:
        gid = db.add_grant("s1", "p", PermissionType.FILESYSTEM, "/x", PermissionScope.ONCE, "@u:t")
        result = db.check_permission("s1", "p", PermissionType.FILESYSTEM, "/x")
        assert result is not None

        db.use_grant(gid)
        result = db.check_permission("s1", "p", PermissionType.FILESYSTEM, "/x")
        assert result is None

    def test_check_pattern_grant(self, db: PermissionDB) -> None:
        db.add_grant(
            "s1", "myapp", PermissionType.FILESYSTEM, "*",
            PermissionScope.PATTERN, "@u:t",
            pattern=r"/home/user/projects/.*",
        )
        result = db.check_permission(
            "s1", "myapp", PermissionType.FILESYSTEM, "/home/user/projects/myapp"
        )
        assert result is not None

        result = db.check_permission(
            "s1", "myapp", PermissionType.FILESYSTEM, "/etc/passwd"
        )
        assert result is None

    def test_check_no_permission(self, db: PermissionDB) -> None:
        result = db.check_permission("s1", "p", PermissionType.FILESYSTEM, "/secret")
        assert result is None

    def test_check_expired_grant(self, db: PermissionDB) -> None:
        db.add_grant(
            "s1", "p", PermissionType.FILESYSTEM, "/tmp",
            PermissionScope.SESSION, "@u:t",
            expires_at=time.time() - 100,
        )
        result = db.check_permission("s1", "p", PermissionType.FILESYSTEM, "/tmp")
        assert result is None

    def test_check_revoked_grant(self, db: PermissionDB) -> None:
        gid = db.add_grant("s1", "p", PermissionType.FILESYSTEM, "/tmp", PermissionScope.SESSION, "@u:t")
        db.revoke_grant(gid, "@u:t")
        result = db.check_permission("s1", "p", PermissionType.FILESYSTEM, "/tmp")
        assert result is None

    def test_check_wrong_type(self, db: PermissionDB) -> None:
        db.add_grant("s1", "p", PermissionType.FILESYSTEM, "/tmp", PermissionScope.SESSION, "@u:t")
        result = db.check_permission("s1", "p", PermissionType.NETWORK, "/tmp")
        assert result is None


# ------------------------------------------------------------------
# Request tests
# ------------------------------------------------------------------


class TestRequests:
    """Test permission request operations."""

    def test_add_and_get_request(self, db: PermissionDB) -> None:
        req_id = db.add_request(
            session_id="s1",
            project_name="myapp",
            perm_type=PermissionType.FILESYSTEM,
            target="/home/user/code",
            reason="Need to read source files",
        )
        req = db.get_request(req_id)

        assert req is not None
        assert req.session_id == "s1"
        assert req.target == "/home/user/code"
        assert req.status == RequestStatus.PENDING

    def test_resolve_request_approved(self, db: PermissionDB) -> None:
        req_id = db.add_request("s1", "p", PermissionType.FILESYSTEM, "/tmp")
        ok = db.resolve_request(req_id, RequestStatus.APPROVED, "@ian:test")
        assert ok is True

        req = db.get_request(req_id)
        assert req is not None
        assert req.status == RequestStatus.APPROVED
        assert req.resolved_by == "@ian:test"

    def test_resolve_request_denied(self, db: PermissionDB) -> None:
        req_id = db.add_request("s1", "p", PermissionType.FILESYSTEM, "/etc")
        db.resolve_request(req_id, RequestStatus.DENIED, "@ian:test")
        req = db.get_request(req_id)
        assert req is not None
        assert req.status == RequestStatus.DENIED

    def test_resolve_already_resolved(self, db: PermissionDB) -> None:
        req_id = db.add_request("s1", "p", PermissionType.FILESYSTEM, "/tmp")
        db.resolve_request(req_id, RequestStatus.APPROVED, "@ian:test")
        ok = db.resolve_request(req_id, RequestStatus.DENIED, "@other:test")
        assert ok is False

    def test_pending_requests(self, db: PermissionDB) -> None:
        db.add_request("s1", "p", PermissionType.FILESYSTEM, "/a")
        db.add_request("s1", "p", PermissionType.FILESYSTEM, "/b")
        db.add_request("s2", "q", PermissionType.NETWORK, "x.com")

        all_pending = db.pending_requests()
        assert len(all_pending) == 3

        s1_pending = db.pending_requests(session_id="s1")
        assert len(s1_pending) == 2

    def test_expire_old_requests(self, db: PermissionDB) -> None:
        # Add a request "from the past"
        db._conn.execute(
            """INSERT INTO requests
            (session_id, project_name, perm_type, target, requested_at)
            VALUES (?, ?, ?, ?, ?)""",
            ("s1", "p", "filesystem", "/old", time.time() - 600),
        )
        db._conn.commit()

        db.add_request("s1", "p", PermissionType.FILESYSTEM, "/new")

        expired = db.expire_old_requests(max_age=300)
        assert expired == 1

        pending = db.pending_requests()
        assert len(pending) == 1
        assert pending[0].target == "/new"


# ------------------------------------------------------------------
# Audit log tests
# ------------------------------------------------------------------


class TestAuditLog:
    """Test audit logging."""

    def test_grant_creates_audit_entry(self, db: PermissionDB) -> None:
        db.add_grant("s1", "p", PermissionType.FILESYSTEM, "/tmp", PermissionScope.SESSION, "@u:t")
        log = db.get_audit_log(session_id="s1")
        assert len(log) == 1
        assert log[0].action == "grant"
        assert log[0].actor == "@u:t"

    def test_revoke_creates_audit_entry(self, db: PermissionDB) -> None:
        gid = db.add_grant("s1", "p", PermissionType.FILESYSTEM, "/tmp", PermissionScope.SESSION, "@u:t")
        db.revoke_grant(gid, "@admin:t")
        log = db.get_audit_log(session_id="s1")
        assert len(log) == 2
        assert log[0].action == "revoke"
        assert log[0].actor == "@admin:t"

    def test_audit_log_limit(self, db: PermissionDB) -> None:
        for i in range(10):
            db.add_grant("s1", "p", PermissionType.FILESYSTEM, f"/{i}", PermissionScope.SESSION, "@u:t")
        log = db.get_audit_log(limit=5)
        assert len(log) == 5

    def test_audit_log_empty(self, db: PermissionDB) -> None:
        log = db.get_audit_log()
        assert log == []


# ------------------------------------------------------------------
# Dataclass tests
# ------------------------------------------------------------------


class TestDataclasses:
    """Test permission dataclass behavior."""

    def test_grant_active_states(self) -> None:
        grant = PermissionGrant(
            id=1, session_id="s", project_name="p",
            perm_type=PermissionType.FILESYSTEM, target="/t",
            scope=PermissionScope.SESSION, pattern=None,
            granted_by="@u:t", granted_at=0, expires_at=None,
            used_count=0, revoked=False,
        )
        assert grant.is_active is True

        grant.revoked = True
        assert grant.is_active is False

    def test_once_grant_deactivates_on_use(self) -> None:
        grant = PermissionGrant(
            id=1, session_id="s", project_name="p",
            perm_type=PermissionType.FILESYSTEM, target="/t",
            scope=PermissionScope.ONCE, pattern=None,
            granted_by="@u:t", granted_at=0, expires_at=None,
            used_count=0, revoked=False,
        )
        assert grant.is_active is True
        grant.used_count = 1
        assert grant.is_active is False

    def test_expired_grant_inactive(self) -> None:
        grant = PermissionGrant(
            id=1, session_id="s", project_name="p",
            perm_type=PermissionType.FILESYSTEM, target="/t",
            scope=PermissionScope.SESSION, pattern=None,
            granted_by="@u:t", granted_at=0,
            expires_at=time.time() - 100,
            used_count=0, revoked=False,
        )
        assert grant.is_active is False
