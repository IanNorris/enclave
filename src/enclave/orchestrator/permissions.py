"""Permission database for Enclave.

SQLite-backed storage for permission grants, pending requests,
and pattern rules. All permissions are managed by the orchestrator
(external to containers).
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any


class PermissionScope(str, Enum):
    """How broadly a permission grant applies."""

    ONCE = "once"           # Single use, auto-revoked after use
    SESSION = "session"     # Valid for the current session only
    PROJECT = "project"     # Valid for any session with this project name
    PATTERN = "pattern"     # Valid for any path matching a regex pattern


class PermissionType(str, Enum):
    """What kind of permission is being granted."""

    FILESYSTEM = "filesystem"   # Access to a file/directory
    NETWORK = "network"         # Access to a network endpoint
    PRIVILEGE = "privilege"     # Root command execution


class RequestStatus(str, Enum):
    """Status of a pending permission request."""

    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"


@dataclass
class PermissionGrant:
    """An active permission grant."""

    id: int
    session_id: str
    project_name: str
    perm_type: PermissionType
    target: str          # path, endpoint, or command
    scope: PermissionScope
    pattern: str | None  # regex pattern for PATTERN scope
    granted_by: str      # Matrix user who approved
    granted_at: float
    expires_at: float | None
    used_count: int
    revoked: bool

    @property
    def is_active(self) -> bool:
        if self.revoked:
            return False
        if self.scope == PermissionScope.ONCE and self.used_count > 0:
            return False
        if self.expires_at and time.time() > self.expires_at:
            return False
        return True


@dataclass
class PermissionRequest:
    """A pending permission request from an agent."""

    id: int
    session_id: str
    project_name: str
    perm_type: PermissionType
    target: str
    reason: str
    status: RequestStatus
    matrix_event_id: str | None  # The approval message event ID
    requested_at: float
    resolved_at: float | None
    resolved_by: str | None


@dataclass
class AuditEntry:
    """An entry in the permission audit log."""

    id: int
    timestamp: float
    session_id: str
    action: str       # grant, deny, revoke, use, expire
    perm_type: str
    target: str
    actor: str        # who performed the action
    details: str


_SCHEMA = """
CREATE TABLE IF NOT EXISTS grants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    project_name TEXT NOT NULL,
    perm_type TEXT NOT NULL,
    target TEXT NOT NULL,
    scope TEXT NOT NULL,
    pattern TEXT,
    granted_by TEXT NOT NULL,
    granted_at REAL NOT NULL,
    expires_at REAL,
    used_count INTEGER DEFAULT 0,
    revoked INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    project_name TEXT NOT NULL,
    perm_type TEXT NOT NULL,
    target TEXT NOT NULL,
    reason TEXT DEFAULT '',
    status TEXT DEFAULT 'pending',
    matrix_event_id TEXT,
    requested_at REAL NOT NULL,
    resolved_at REAL,
    resolved_by TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    session_id TEXT NOT NULL,
    action TEXT NOT NULL,
    perm_type TEXT NOT NULL,
    target TEXT NOT NULL,
    actor TEXT NOT NULL,
    details TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_grants_session ON grants(session_id);
CREATE INDEX IF NOT EXISTS idx_grants_project ON grants(project_name);
CREATE INDEX IF NOT EXISTS idx_grants_active ON grants(revoked, scope);
CREATE INDEX IF NOT EXISTS idx_requests_status ON requests(status);
CREATE INDEX IF NOT EXISTS idx_requests_session ON requests(session_id);
CREATE INDEX IF NOT EXISTS idx_audit_session ON audit_log(session_id);
"""


class PermissionDB:
    """SQLite-backed permission storage.

    Thread-safe via SQLite's WAL mode. All operations are synchronous
    (SQLite is fast enough for this use case).
    """

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------
    # Grants
    # ------------------------------------------------------------------

    def add_grant(
        self,
        session_id: str,
        project_name: str,
        perm_type: PermissionType,
        target: str,
        scope: PermissionScope,
        granted_by: str,
        pattern: str | None = None,
        expires_at: float | None = None,
    ) -> int:
        """Add a new permission grant. Returns the grant ID."""
        cursor = self._conn.execute(
            """INSERT INTO grants
            (session_id, project_name, perm_type, target, scope, pattern,
             granted_by, granted_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id,
                project_name,
                perm_type.value,
                target,
                scope.value,
                pattern,
                granted_by,
                time.time(),
                expires_at,
            ),
        )
        self._conn.commit()
        grant_id = cursor.lastrowid
        assert grant_id is not None

        self._audit(
            session_id, "grant", perm_type.value, target, granted_by,
            f"scope={scope.value}, pattern={pattern}",
        )
        return grant_id

    def get_grant(self, grant_id: int) -> PermissionGrant | None:
        """Get a grant by ID."""
        row = self._conn.execute(
            "SELECT * FROM grants WHERE id = ?", (grant_id,)
        ).fetchone()
        return self._row_to_grant(row) if row else None

    def check_permission(
        self,
        session_id: str,
        project_name: str,
        perm_type: PermissionType,
        target: str,
    ) -> PermissionGrant | None:
        """Check if a permission is granted. Returns the matching grant or None.

        Checks in order of specificity: once > session > project > pattern.
        """
        now = time.time()

        # Check session-specific grants
        rows = self._conn.execute(
            """SELECT * FROM grants
            WHERE revoked = 0
            AND perm_type = ?
            AND (
                (scope = 'once' AND session_id = ? AND target = ? AND used_count = 0)
                OR (scope = 'session' AND session_id = ? AND target = ?)
                OR (scope = 'project' AND project_name = ? AND target = ?)
            )
            AND (expires_at IS NULL OR expires_at > ?)
            ORDER BY
                CASE scope
                    WHEN 'once' THEN 1
                    WHEN 'session' THEN 2
                    WHEN 'project' THEN 3
                END
            LIMIT 1""",
            (
                perm_type.value,
                session_id, target,
                session_id, target,
                project_name, target,
                now,
            ),
        ).fetchall()

        if rows:
            return self._row_to_grant(rows[0])

        # Check pattern grants
        import re
        pattern_rows = self._conn.execute(
            """SELECT * FROM grants
            WHERE revoked = 0 AND scope = 'pattern' AND perm_type = ?
            AND project_name = ?
            AND (expires_at IS NULL OR expires_at > ?)""",
            (perm_type.value, project_name, now),
        ).fetchall()

        for row in pattern_rows:
            try:
                if row["pattern"] and re.match(row["pattern"], target):
                    return self._row_to_grant(row)
            except re.error:
                continue

        return None

    def use_grant(self, grant_id: int) -> None:
        """Mark a grant as used (increments use count)."""
        self._conn.execute(
            "UPDATE grants SET used_count = used_count + 1 WHERE id = ?",
            (grant_id,),
        )
        self._conn.commit()

    def revoke_grant(self, grant_id: int, revoked_by: str) -> bool:
        """Revoke a grant. Returns True if found."""
        cursor = self._conn.execute(
            "UPDATE grants SET revoked = 1 WHERE id = ? AND revoked = 0",
            (grant_id,),
        )
        self._conn.commit()
        if cursor.rowcount > 0:
            grant = self.get_grant(grant_id)
            if grant:
                self._audit(
                    grant.session_id, "revoke", grant.perm_type.value,
                    grant.target, revoked_by, f"grant_id={grant_id}",
                )
            return True
        return False

    def list_grants(
        self,
        session_id: str | None = None,
        project_name: str | None = None,
        active_only: bool = True,
    ) -> list[PermissionGrant]:
        """List grants with optional filters."""
        conditions = []
        params: list[Any] = []

        if active_only:
            conditions.append("revoked = 0")
        if session_id:
            conditions.append("session_id = ?")
            params.append(session_id)
        if project_name:
            conditions.append("project_name = ?")
            params.append(project_name)

        where = " AND ".join(conditions) if conditions else "1=1"
        rows = self._conn.execute(
            f"SELECT * FROM grants WHERE {where} ORDER BY granted_at DESC",
            params,
        ).fetchall()
        return [self._row_to_grant(r) for r in rows]

    def revoke_session_grants(self, session_id: str, revoked_by: str) -> int:
        """Revoke all active grants for a session. Returns count revoked."""
        cursor = self._conn.execute(
            "UPDATE grants SET revoked = 1 WHERE session_id = ? AND revoked = 0 AND scope = 'session'",
            (session_id,),
        )
        self._conn.commit()
        count = cursor.rowcount
        if count > 0:
            self._audit(
                session_id, "revoke_session", "all", "all", revoked_by,
                f"revoked {count} session grants",
            )
        return count

    # ------------------------------------------------------------------
    # Requests
    # ------------------------------------------------------------------

    def add_request(
        self,
        session_id: str,
        project_name: str,
        perm_type: PermissionType,
        target: str,
        reason: str = "",
        matrix_event_id: str | None = None,
    ) -> int:
        """Add a pending permission request. Returns request ID."""
        cursor = self._conn.execute(
            """INSERT INTO requests
            (session_id, project_name, perm_type, target, reason,
             matrix_event_id, requested_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id,
                project_name,
                perm_type.value,
                target,
                reason,
                matrix_event_id,
                time.time(),
            ),
        )
        self._conn.commit()
        return cursor.lastrowid or 0

    def get_request(self, request_id: int) -> PermissionRequest | None:
        """Get a request by ID."""
        row = self._conn.execute(
            "SELECT * FROM requests WHERE id = ?", (request_id,)
        ).fetchone()
        return self._row_to_request(row) if row else None

    def resolve_request(
        self,
        request_id: int,
        status: RequestStatus,
        resolved_by: str,
    ) -> bool:
        """Resolve a pending request. Returns True if found."""
        cursor = self._conn.execute(
            """UPDATE requests
            SET status = ?, resolved_at = ?, resolved_by = ?
            WHERE id = ? AND status = 'pending'""",
            (status.value, time.time(), resolved_by, request_id),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def pending_requests(
        self, session_id: str | None = None
    ) -> list[PermissionRequest]:
        """List pending requests."""
        if session_id:
            rows = self._conn.execute(
                "SELECT * FROM requests WHERE status = 'pending' AND session_id = ?",
                (session_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM requests WHERE status = 'pending'"
            ).fetchall()
        return [self._row_to_request(r) for r in rows]

    def expire_old_requests(self, max_age: float = 300.0) -> int:
        """Expire pending requests older than max_age seconds."""
        cutoff = time.time() - max_age
        cursor = self._conn.execute(
            """UPDATE requests SET status = 'expired', resolved_at = ?
            WHERE status = 'pending' AND requested_at < ?""",
            (time.time(), cutoff),
        )
        self._conn.commit()
        return cursor.rowcount

    # ------------------------------------------------------------------
    # Audit log
    # ------------------------------------------------------------------

    def get_audit_log(
        self,
        session_id: str | None = None,
        limit: int = 100,
    ) -> list[AuditEntry]:
        """Get recent audit entries."""
        if session_id:
            rows = self._conn.execute(
                "SELECT * FROM audit_log WHERE session_id = ? ORDER BY timestamp DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_audit(r) for r in rows]

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _audit(
        self,
        session_id: str,
        action: str,
        perm_type: str,
        target: str,
        actor: str,
        details: str = "",
    ) -> None:
        self._conn.execute(
            """INSERT INTO audit_log
            (timestamp, session_id, action, perm_type, target, actor, details)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (time.time(), session_id, action, perm_type, target, actor, details),
        )
        self._conn.commit()

    @staticmethod
    def _row_to_grant(row: sqlite3.Row) -> PermissionGrant:
        return PermissionGrant(
            id=row["id"],
            session_id=row["session_id"],
            project_name=row["project_name"],
            perm_type=PermissionType(row["perm_type"]),
            target=row["target"],
            scope=PermissionScope(row["scope"]),
            pattern=row["pattern"],
            granted_by=row["granted_by"],
            granted_at=row["granted_at"],
            expires_at=row["expires_at"],
            used_count=row["used_count"],
            revoked=bool(row["revoked"]),
        )

    @staticmethod
    def _row_to_request(row: sqlite3.Row) -> PermissionRequest:
        return PermissionRequest(
            id=row["id"],
            session_id=row["session_id"],
            project_name=row["project_name"],
            perm_type=PermissionType(row["perm_type"]),
            target=row["target"],
            reason=row["reason"],
            status=RequestStatus(row["status"]),
            matrix_event_id=row["matrix_event_id"],
            requested_at=row["requested_at"],
            resolved_at=row["resolved_at"],
            resolved_by=row["resolved_by"],
        )

    @staticmethod
    def _row_to_audit(row: sqlite3.Row) -> AuditEntry:
        return AuditEntry(
            id=row["id"],
            timestamp=row["timestamp"],
            session_id=row["session_id"],
            action=row["action"],
            perm_type=row["perm_type"],
            target=row["target"],
            actor=row["actor"],
            details=row["details"],
        )
