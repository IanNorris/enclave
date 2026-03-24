"""Approval flow: reaction-based permission approval via Matrix.

Posts permission requests to Matrix with emoji options. Users react to
approve/deny. The orchestrator watches for reactions and resolves requests.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Callable, Awaitable

from enclave.common.logging import get_logger
from enclave.orchestrator.permissions import (
    PermissionDB,
    PermissionScope,
    PermissionType,
    RequestStatus,
)

log = get_logger("approval")

# Emoji for approval options
EMOJI_ONCE = "1\ufe0f\u20e3"       # 1️⃣ Approve once
EMOJI_SESSION = "2\ufe0f\u20e3"    # 2️⃣ Approve for session
EMOJI_PROJECT = "3\ufe0f\u20e3"    # 3️⃣ Approve for project
EMOJI_PATTERN = "4\ufe0f\u20e3"    # 4️⃣ Approve with pattern
EMOJI_DENY = "\u274c"              # ❌ Deny

SCOPE_MAP = {
    EMOJI_ONCE: PermissionScope.ONCE,
    EMOJI_SESSION: PermissionScope.SESSION,
    EMOJI_PROJECT: PermissionScope.PROJECT,
    EMOJI_PATTERN: PermissionScope.PATTERN,
}


def suggest_pattern(target: str) -> str:
    """Suggest a regex pattern for a filesystem path.

    For example: /home/user/projects/myapp → /home/user/projects/.*
    """
    parts = target.rstrip("/").rsplit("/", 1)
    if len(parts) == 2:
        return re.escape(parts[0]) + "/.*"
    return re.escape(target)


def format_approval_message(
    session_name: str,
    perm_type: PermissionType,
    target: str,
    reason: str,
    request_id: int,
) -> tuple[str, str]:
    """Format the approval request message for Matrix.

    Returns (plain_text, html) tuple.
    """
    type_icon = {
        PermissionType.FILESYSTEM: "📂",
        PermissionType.NETWORK: "🌐",
        PermissionType.PRIVILEGE: "🔐",
    }.get(perm_type, "❓")

    suggested = suggest_pattern(target)

    plain = (
        f"🔒 Permission Request #{request_id}\n\n"
        f"{type_icon} {perm_type.value.title()}: {target}\n"
        f"📦 Session: {session_name}\n"
    )
    if reason:
        plain += f"💬 Reason: {reason}\n"

    plain += (
        f"\nReact to approve:\n"
        f"  {EMOJI_ONCE} Approve once\n"
        f"  {EMOJI_SESSION} Approve for this session\n"
        f"  {EMOJI_PROJECT} Approve for all sessions of this project\n"
        f"  {EMOJI_PATTERN} Approve pattern: `{suggested}`\n"
        f"  {EMOJI_DENY} Deny\n"
    )

    html = (
        f"<h4>🔒 Permission Request #{request_id}</h4>"
        f"<p>{type_icon} <b>{perm_type.value.title()}</b>: <code>{target}</code><br/>"
        f"📦 <b>Session</b>: {session_name}</p>"
    )
    if reason:
        html += f"<p>💬 <b>Reason</b>: {reason}</p>"

    html += (
        f"<p>React to approve:<br/>"
        f"&nbsp;&nbsp;{EMOJI_ONCE} Approve once<br/>"
        f"&nbsp;&nbsp;{EMOJI_SESSION} Approve for this session<br/>"
        f"&nbsp;&nbsp;{EMOJI_PROJECT} Approve for all sessions of this project<br/>"
        f"&nbsp;&nbsp;{EMOJI_PATTERN} Approve pattern: <code>{suggested}</code><br/>"
        f"&nbsp;&nbsp;{EMOJI_DENY} Deny</p>"
    )

    return plain, html


# Type for the Matrix send function
SendMessageFn = Callable[..., Awaitable[str | None]]
SendReactionFn = Callable[..., Awaitable[str | None]]


class ApprovalManager:
    """Manages the reaction-based approval flow.

    Posts requests to Matrix, seeds emoji reactions, and watches for
    user reactions to resolve permissions.
    """

    def __init__(
        self,
        permission_db: PermissionDB,
        send_message: SendMessageFn,
        send_reaction: SendReactionFn,
        approval_room_id: str,
        timeout: float = 300.0,
    ):
        self.db = permission_db
        self.send_message = send_message
        self.send_reaction = send_reaction
        self.approval_room_id = approval_room_id
        self.timeout = timeout

        # Map: matrix_event_id → request_id
        self._pending: dict[str, int] = {}
        # Map: request_id → asyncio.Event (for blocking callers)
        self._events: dict[int, asyncio.Event] = {}

    async def request_permission(
        self,
        session_id: str,
        session_name: str,
        project_name: str,
        perm_type: PermissionType,
        target: str,
        reason: str = "",
    ) -> tuple[RequestStatus, PermissionScope | None]:
        """Post a permission request and wait for user response.

        Returns (status, scope) — scope is None if denied/expired.
        """
        # Check if already granted
        existing = self.db.check_permission(
            session_id, project_name, perm_type, target
        )
        if existing:
            self.db.use_grant(existing.id)
            return RequestStatus.APPROVED, existing.scope

        # Create request in DB
        request_id = self.db.add_request(
            session_id=session_id,
            project_name=project_name,
            perm_type=perm_type,
            target=target,
            reason=reason,
        )

        # Post to Matrix
        plain, html = format_approval_message(
            session_name, perm_type, target, reason, request_id
        )

        event_id = await self.send_message(
            self.approval_room_id, plain, html_body=html
        )

        if event_id is None:
            self.db.resolve_request(request_id, RequestStatus.EXPIRED, "system")
            return RequestStatus.EXPIRED, None

        # Update request with event ID
        self.db._conn.execute(
            "UPDATE requests SET matrix_event_id = ? WHERE id = ?",
            (event_id, request_id),
        )
        self.db._conn.commit()

        # Seed emoji reactions
        for emoji in [EMOJI_ONCE, EMOJI_SESSION, EMOJI_PROJECT, EMOJI_PATTERN, EMOJI_DENY]:
            await self.send_reaction(self.approval_room_id, event_id, emoji)

        # Register pending
        self._pending[event_id] = request_id
        wait_event = asyncio.Event()
        self._events[request_id] = wait_event

        # Wait for response
        try:
            await asyncio.wait_for(wait_event.wait(), timeout=self.timeout)
        except asyncio.TimeoutError:
            self.db.resolve_request(request_id, RequestStatus.EXPIRED, "system")
            self._cleanup(event_id, request_id)
            return RequestStatus.EXPIRED, None

        # Get result
        req = self.db.get_request(request_id)
        self._cleanup(event_id, request_id)

        if req and req.status == RequestStatus.APPROVED:
            return RequestStatus.APPROVED, None  # scope set by caller
        return RequestStatus.DENIED, None

    def handle_reaction(
        self,
        event_id: str,
        emoji: str,
        sender: str,
    ) -> tuple[int | None, PermissionScope | None]:
        """Handle a Matrix reaction on an approval message.

        Returns (request_id, scope) if this resolved a request, else (None, None).
        """
        request_id = self._pending.get(event_id)
        if request_id is None:
            return None, None

        if emoji == EMOJI_DENY:
            self.db.resolve_request(request_id, RequestStatus.DENIED, sender)
            if request_id in self._events:
                self._events[request_id].set()
            return request_id, None

        scope = SCOPE_MAP.get(emoji)
        if scope is None:
            return None, None

        self.db.resolve_request(request_id, RequestStatus.APPROVED, sender)
        if request_id in self._events:
            self._events[request_id].set()

        return request_id, scope

    def _cleanup(self, event_id: str, request_id: int) -> None:
        self._pending.pop(event_id, None)
        self._events.pop(request_id, None)
