"""Null-object Matrix client for web-UI-only (Matrix-disabled) operation.

When ``matrix.enabled`` is false the orchestrator wires ``self.matrix`` to a
``NullMatrixClient`` instead of :class:`EnclaveMatrixClient`. This keeps the
~28-method Matrix surface callable everywhere (the ~81 call sites in
``router.py`` stay unchanged) without importing ``matrix-nio`` at all.

Behaviour follows a deliberate split:

* **Fire-and-forget** operations (typing, reactions, notifications, handler
  registration, room membership, ``cleanup_room``, ``close``) are true no-ops.
* **Correlated / awaited results** (``create_room`` ids, ``send_poll`` /
  ``end_poll`` event ids, ``upload_file`` / ``download_media``) return the
  honest "nothing happened" value (``None`` / ``False``) rather than fabricating
  a plausible Matrix id. Callers that need a real room id (session creation)
  branch on ``matrix.enabled`` and mint a synthetic ``local:<uuid>`` id instead
  of calling ``create_room`` here.

``matrix-nio`` is never imported by this module, so Enclave runs with the
dependency absent when Matrix is disabled.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Awaitable, Callable

from enclave.common.logging import get_logger

log = get_logger("matrix.null")


class _NullNioClient:
    """Stand-in for the nio ``AsyncClient`` attribute (``matrix.client``).

    Only the members the orchestrator actually reads are provided: an empty
    ``rooms`` mapping and ``logged_in = False``.
    """

    rooms: dict[str, Any] = {}
    logged_in: bool = False


class NullMatrixClient:
    """No-op Matrix client used when Matrix is disabled (web-UI-only mode)."""

    #: Distinguishes this from the real client for the few genuinely
    #: Matrix-specific branches (startup announce, status display).
    enabled: bool = False

    def __init__(self) -> None:
        self.client = _NullNioClient()

    # ── Handler registration (no-op: no events will ever arrive) ──
    def on_message(self, handler: Callable[..., Any]) -> None:
        return None

    def on_user_join(self, handler: Callable[..., Any]) -> None:
        return None

    def on_reaction(self, handler: Callable[..., Any]) -> None:
        return None

    def on_poll_response(self, handler: Callable[..., Any]) -> None:
        return None

    # ── Lifecycle ──
    async def login(self) -> bool:
        # Never called in the disabled path (main.py skips the login gate),
        # but harmless if it is: report success so nothing hard-fails.
        return True

    async def initial_sync(self) -> None:
        return None

    async def sync_forever(self, timeout: int = 30000) -> None:
        # Must return immediately — the disabled path never schedules this,
        # and if it ever is scheduled it must not block the loop forever.
        return None

    def stop_sync(self) -> None:
        return None

    async def close(self) -> None:
        return None

    # ── Messaging (fire-and-forget) ──
    async def send_message(
        self,
        room_id: str,
        body: str,
        html_body: str | None = None,
        thread_event_id: str | None = None,
    ) -> str | None:
        return None

    async def send_reaction(
        self, room_id: str, event_id: str, emoji: str
    ) -> str | None:
        return None

    async def edit_message(
        self,
        room_id: str,
        event_id: str,
        body: str,
        html_body: str | None = None,
    ) -> str | None:
        return None

    async def redact_event(
        self, room_id: str, event_id: str, reason: str = ""
    ) -> bool:
        return False

    async def set_typing(
        self, room_id: str, typing: bool = True, timeout: int = 30000
    ) -> None:
        return None

    # ── Polls (correlated: honest "not posted" → None) ──
    async def send_poll(
        self,
        room_id: str,
        question: str,
        answers: list[tuple[str, str]],
        thread_event_id: str | None = None,
    ) -> str | None:
        return None

    async def end_poll(
        self, room_id: str, poll_event_id: str
    ) -> str | None:
        return None

    # ── Event counting ──
    def get_event_count(self, room_id: str) -> int:
        return 0

    def reset_event_count(self, room_id: str) -> None:
        return None

    async def purge_room_history(
        self, room_id: str, keep_events: int = 200
    ) -> int:
        return 0

    # ── Rooms (correlated ids: honest None; membership: no-op) ──
    async def create_room(
        self,
        name: str,
        topic: str = "",
        invite: list[str] | None = None,
        encrypted: bool = True,
        space_id: str | None = None,
    ) -> str | None:
        # Fail-fast, not fake-success: session creation must branch on
        # matrix.enabled and mint a synthetic room id itself.
        log.warning("create_room called with Matrix disabled — returning None")
        return None

    async def create_space(self, name: str) -> str | None:
        return None

    async def join_room(self, room_id: str) -> bool:
        return False

    async def leave_room(self, room_id: str) -> bool:
        return False

    async def forget_room(self, room_id: str) -> bool:
        return False

    async def invite_user(self, room_id: str, user_id: str) -> bool:
        return False

    async def kick_user(
        self, room_id: str, user_id: str, reason: str = ""
    ) -> bool:
        return False

    async def cleanup_room(
        self, room_id: str, user_ids: list[str] | None = None, reason: str = ""
    ) -> bool:
        return False

    async def _trust_users(self, user_ids: list[str]) -> None:
        return None

    # ── Media (correlated / IO: honest failure) ──
    async def download_media(
        self,
        mxc_url: str,
        dest: str | Path,
        encryption: dict[str, Any] | None = None,
    ) -> bool:
        return False

    async def upload_file(
        self, room_id: str, file_path: str | Path, body: str | None = None
    ) -> str | None:
        return None
