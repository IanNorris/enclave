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
        # Optional async callback to surface a pending request in the web UI
        # (and to clear it on resolve). Set by the router. Signature:
        #   async on_ui_request(event: dict) -> None
        # where event is {"kind": "request"|"resolved", "session_id", ...}.
        self.on_ui_request: Callable[[dict], Awaitable[None]] | None = None

        # Map: poll_event_id → (request_id, suggested_pattern)
        self._pending: dict[str, tuple[int, str]] = {}
        # Map: request_id → asyncio.Event (for blocking callers)
        self._events: dict[int, asyncio.Event] = {}
        # Map: request_id → resolved (status, scope, pattern)
        self._results: dict[int, tuple[RequestStatus, PermissionScope | None, str | None]] = {}
        # Requests awaiting custom pattern text input
        # request_id → (room_id, suggested_pattern)
        self._awaiting_pattern: dict[int, tuple[str, str]] = {}
        # Map: request_id → the suggested pattern, so a web-UI response (keyed by
        # request_id, not poll_event_id) can resolve a pattern grant.
        self._request_pattern: dict[int, str] = {}

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
        allow_pattern: bool = True,
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
        ]
        # Pattern grants apply a regex to FUTURE targets, so they must never be
        # offered for host-command / GUI launches: such a target's leading token
        # is a constant category prefix (e.g. "GUI:") which would yield a pattern
        # that auto-approves *every* future command → host RCE. Callers pass
        # allow_pattern=False to suppress both pattern options for those cases.
        if allow_pattern:
            answers.append(
                (ANSWER_APPROVE_PATTERN, f"✅ Approve pattern: {pattern}")
            )
            answers.append(
                (ANSWER_CUSTOM_PATTERN, "✏️ Approve with custom pattern")
            )
        answers.extend([
            (ANSWER_DENY_ONCE, "❌ Deny once"),
            (ANSWER_DENY_PROJECT, "❌ Deny for project"),
        ])

        # Register the waiter + per-request metadata BEFORE surfacing the request
        # anywhere, so a fast web-UI response can't race ahead of registration.
        wait_event = asyncio.Event()
        self._events[request_id] = wait_event
        self._request_pattern[request_id] = pattern

        # Surface the request to the web UI (the primary, Matrix-independent
        # channel). A browser resolves it by request_id via resolve_external.
        if self.on_ui_request is not None:
            try:
                await self.on_ui_request({
                    "kind": "request",
                    "session_id": session_id,
                    "request_id": request_id,
                    "perm_type": perm_type.value,
                    "target": target,
                    "reason": reason,
                    "pattern": pattern if allow_pattern else "",
                    "allow_pattern": allow_pattern,
                    "timeout": self.timeout,
                })
            except Exception as e:
                log.warning("on_ui_request(request) failed: %s", e)

        # Additionally post a Matrix poll when a room is available (Matrix on).
        poll_event_id = None
        if room_id and self.send_poll is not None:
            poll_event_id = await self.send_poll(room_id, question, answers)
            if poll_event_id is not None:
                self.db._conn.execute(
                    "UPDATE requests SET matrix_event_id = ? WHERE id = ?",
                    (poll_event_id, request_id),
                )
                self.db._conn.commit()
                self._pending[poll_event_id] = (request_id, pattern)

        # With neither a web-UI channel nor a Matrix poll, nobody can answer;
        # fail closed immediately rather than blocking for the full timeout.
        if self.on_ui_request is None and poll_event_id is None:
            log.error("No approval channel (no web UI, no Matrix room) — denying")
            self.db.resolve_request(request_id, RequestStatus.EXPIRED, "system")
            self._cleanup(poll_event_id, request_id)
            return RequestStatus.EXPIRED, None, None

        # Wait for a response from either channel.
        try:
            await asyncio.wait_for(wait_event.wait(), timeout=self.timeout)
        except asyncio.TimeoutError:
            self.db.resolve_request(request_id, RequestStatus.EXPIRED, "system")
            await self._notify_ui_resolved(session_id, request_id, "expired")
            self._cleanup(poll_event_id, request_id)
            return RequestStatus.EXPIRED, None, None

        # Close the Matrix poll if one was posted.
        if poll_event_id is not None and room_id:
            try:
                await self.end_poll(room_id, poll_event_id)
            except Exception:
                pass

        # Get result
        result = self._results.pop(request_id, None)
        await self._notify_ui_resolved(session_id, request_id, "resolved")
        self._cleanup(poll_event_id, request_id)

        if result:
            return result
        return RequestStatus.DENIED, None, None

    async def _notify_ui_resolved(self, session_id: str, request_id: int, why: str) -> None:
        """Tell the web UI to clear a request card (answered elsewhere/expired)."""
        if self.on_ui_request is None:
            return
        try:
            await self.on_ui_request({
                "kind": "resolved",
                "session_id": session_id,
                "request_id": request_id,
                "why": why,
            })
        except Exception as e:
            log.warning("on_ui_request(resolved) failed: %s", e)

    def resolve_external(
        self, request_id: int, answer_id: str, sender: str,
    ) -> bool:
        """Resolve a pending request by request_id (web-UI response path).

        Returns True if a waiting request was resolved. Mirrors
        ``handle_poll_response`` but is keyed on request_id rather than a Matrix
        poll event, so it works with Matrix disabled.
        """
        if request_id not in self._events:
            return False
        mapping = ANSWER_MAP.get(answer_id)
        if mapping is None:
            return False
        status, scope = mapping
        pattern = self._request_pattern.get(request_id, "")
        result_pattern = pattern if answer_id == ANSWER_APPROVE_PATTERN else None
        self.db.resolve_request(request_id, status, sender)
        self._results[request_id] = (status, scope, result_pattern)
        self._events[request_id].set()
        return True

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
        self._request_pattern.pop(request_id, None)
