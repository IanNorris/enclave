"""Approval flow: poll-based permission approval via Matrix.

Posts permission requests as polls in the project room. Users vote to
approve/deny. The orchestrator watches for poll responses and resolves requests.
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

# Poll answer IDs
ANSWER_APPROVE_ONCE = "approve_once"
ANSWER_APPROVE_PROJECT = "approve_project"
ANSWER_APPROVE_PATTERN = "approve_pattern"
ANSWER_CUSTOM_PATTERN = "custom_pattern"
ANSWER_DENY_ONCE = "deny_once"
ANSWER_DENY_PROJECT = "deny_project"

# Maps answer IDs to (status, scope)
ANSWER_MAP: dict[str, tuple[RequestStatus, PermissionScope | None]] = {
    ANSWER_APPROVE_ONCE: (RequestStatus.APPROVED, PermissionScope.ONCE),
    ANSWER_APPROVE_PROJECT: (RequestStatus.APPROVED, PermissionScope.PROJECT),
    ANSWER_APPROVE_PATTERN: (RequestStatus.APPROVED, PermissionScope.PATTERN),
    ANSWER_CUSTOM_PATTERN: (RequestStatus.APPROVED, PermissionScope.PATTERN),
    ANSWER_DENY_ONCE: (RequestStatus.DENIED, None),
    ANSWER_DENY_PROJECT: (RequestStatus.DENIED, PermissionScope.PROJECT),
}


def suggest_pattern(target: str) -> str:
    """Suggest a regex pattern for a command or path.

    For commands: apt-get install -y cowsay → ^apt-get\\s+
    For paths: /home/user/projects/myapp → /home/user/projects/.*
    """
    # If it looks like a command (no leading /), pattern on first word
    if not target.startswith("/"):
        first = target.split()[0] if target.strip() else target
        return f"^{re.escape(first)}\\s+"
    # For paths, generalize the last component
    parts = target.rstrip("/").rsplit("/", 1)
    if len(parts) == 2:
        return re.escape(parts[0]) + "/.*"
    return re.escape(target)


# Type for the Matrix send functions
SendMessageFn = Callable[..., Awaitable[str | None]]
SendReactionFn = Callable[..., Awaitable[str | None]]
SendPollFn = Callable[..., Awaitable[str | None]]
EndPollFn = Callable[..., Awaitable[str | None]]


class ApprovalManager:
    """Manages the poll-based approval flow.

    Posts requests as polls in the project room and watches for
    poll responses to resolve permissions.
    """

    def __init__(
        self,
        permission_db: PermissionDB,
        send_message: SendMessageFn,
        send_reaction: SendReactionFn,
        send_poll: SendPollFn,
        end_poll: EndPollFn,
        timeout: float = 300.0,
    ):
        self.db = permission_db
        self.send_message = send_message
        self.send_reaction = send_reaction
        self.send_poll = send_poll
        self.end_poll = end_poll
        self.timeout = timeout

        # Map: poll_event_id → (request_id, suggested_pattern)
        self._pending: dict[str, tuple[int, str]] = {}
        # Map: request_id → asyncio.Event (for blocking callers)
        self._events: dict[int, asyncio.Event] = {}
        # Map: request_id → resolved (status, scope, pattern)
        self._results: dict[int, tuple[RequestStatus, PermissionScope | None, str | None]] = {}
        # Requests awaiting custom pattern text input
        # request_id → (room_id, suggested_pattern)
        self._awaiting_pattern: dict[int, tuple[str, str]] = {}

    async def request_permission(
        self,
        session_id: str,
        session_name: str,
        project_name: str,
        perm_type: PermissionType,
        target: str,
        reason: str = "",
        room_id: str | None = None,
        suggested_pattern: str | None = None,
    ) -> tuple[RequestStatus, PermissionScope | None, str | None]:
        """Post a permission request poll and wait for user response.

        Args:
            room_id: Room to post the poll in (project room).
            suggested_pattern: Agent-suggested regex pattern (optional).

        Returns (status, scope, pattern) — scope/pattern None if denied/expired.
        """
        # Check if already granted
        existing = self.db.check_permission(
            session_id, project_name, perm_type, target
        )
        if existing:
            self.db.use_grant(existing.id)
            return RequestStatus.APPROVED, existing.scope, None

        if not room_id:
            log.error("No room_id for approval request")
            return RequestStatus.EXPIRED, None, None

        # Create request in DB
        request_id = self.db.add_request(
            session_id=session_id,
            project_name=project_name,
            perm_type=perm_type,
            target=target,
            reason=reason,
        )

        # Build suggested pattern
        pattern = suggested_pattern or suggest_pattern(target)

        # Build poll question & answers
        type_icon = {
            PermissionType.FILESYSTEM: "📂",
            PermissionType.NETWORK: "🌐",
        }.get(perm_type, "❓")

        question = (
            f"{type_icon} {perm_type.value.title()}: `{target}`"
        )
        if reason:
            question += f"\n💬 {reason}"

        answers = [
            (ANSWER_APPROVE_ONCE, "✅ Approve once"),
            (ANSWER_APPROVE_PROJECT, "✅ Approve for project"),
            (ANSWER_APPROVE_PATTERN, f"✅ Approve pattern: {pattern}"),
            (ANSWER_CUSTOM_PATTERN, "✏️ Approve with custom pattern"),
            (ANSWER_DENY_ONCE, "❌ Deny once"),
            (ANSWER_DENY_PROJECT, "❌ Deny for project"),
        ]

        poll_event_id = await self.send_poll(room_id, question, answers)

        if poll_event_id is None:
            self.db.resolve_request(request_id, RequestStatus.EXPIRED, "system")
            return RequestStatus.EXPIRED, None, None

        # Update request with event ID
        self.db._conn.execute(
            "UPDATE requests SET matrix_event_id = ? WHERE id = ?",
            (poll_event_id, request_id),
        )
        self.db._conn.commit()

        # Register pending
        self._pending[poll_event_id] = (request_id, pattern)
        wait_event = asyncio.Event()
        self._events[request_id] = wait_event

        # Wait for response
        try:
            await asyncio.wait_for(wait_event.wait(), timeout=self.timeout)
        except asyncio.TimeoutError:
            self.db.resolve_request(request_id, RequestStatus.EXPIRED, "system")
            self._cleanup(poll_event_id, request_id)
            return RequestStatus.EXPIRED, None, None

        # Close the poll
        try:
            await self.end_poll(room_id, poll_event_id)
        except Exception:
            pass

        # Get result
        result = self._results.pop(request_id, None)
        self._cleanup(poll_event_id, request_id)

        if result:
            return result
        return RequestStatus.DENIED, None, None

    def handle_poll_response(
        self,
        poll_event_id: str,
        answer_ids: list[str],
        sender: str,
        room_id: str,
    ) -> tuple[int | None, PermissionScope | None, bool]:
        """Handle a poll response.

        Returns (request_id, scope, needs_custom_pattern).
        """
        pending = self._pending.get(poll_event_id)
        if pending is None:
            return None, None, False

        request_id, suggested_pattern = pending

        if not answer_ids:
            return None, None, False

        answer_id = answer_ids[0]  # Single-select poll
        mapping = ANSWER_MAP.get(answer_id)
        if mapping is None:
            return None, None, False

        status, scope = mapping

        # "Custom pattern" — need follow-up text input
        if answer_id == ANSWER_CUSTOM_PATTERN:
            self._awaiting_pattern[request_id] = (room_id, suggested_pattern)
            return request_id, PermissionScope.PATTERN, True

        # Determine pattern for the pattern option
        result_pattern = suggested_pattern if answer_id == ANSWER_APPROVE_PATTERN else None

        self.db.resolve_request(request_id, status, sender)
        self._results[request_id] = (status, scope, result_pattern)
        if request_id in self._events:
            self._events[request_id].set()

        return request_id, scope, False

    def handle_custom_pattern(
        self,
        request_id: int,
        pattern: str,
        sender: str,
    ) -> bool:
        """Handle the custom pattern text reply.

        Returns True if the request was resolved.
        """
        if request_id not in self._awaiting_pattern:
            return False

        self._awaiting_pattern.pop(request_id, None)
        self.db.resolve_request(request_id, RequestStatus.APPROVED, sender)
        self._results[request_id] = (
            RequestStatus.APPROVED,
            PermissionScope.PATTERN,
            pattern.strip(),
        )
        if request_id in self._events:
            self._events[request_id].set()
        return True

    def get_awaiting_pattern(self, room_id: str) -> int | None:
        """Check if there's a request awaiting a custom pattern in this room."""
        for req_id, (rid, _) in self._awaiting_pattern.items():
            if rid == room_id:
                return req_id
        return None

    # Keep reaction handler for backwards compatibility
    def handle_reaction(
        self,
        event_id: str,
        emoji: str,
        sender: str,
    ) -> tuple[int | None, PermissionScope | None]:
        """Handle a Matrix reaction on an approval message (legacy)."""
        return None, None

    def _cleanup(self, event_id: str, request_id: int) -> None:
        self._pending.pop(event_id, None)
        self._events.pop(request_id, None)
        self._results.pop(request_id, None)
