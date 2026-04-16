"""Matrix client wrapper for Enclave orchestrator.

Handles E2EE, persistent device store, device trust, room management,
and message sending/receiving. Wraps matrix-nio with Enclave-specific
convenience methods.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Callable, Awaitable

import mimetypes

from nio import (
    AsyncClient,
    AsyncClientConfig,
    DownloadResponse,
    Event,
    InviteMemberEvent,
    LocalProtocolError,
    LoginResponse,
    MatrixRoom,
    MegolmEvent,
    RoomCreateResponse,
    RoomEncryptionEvent,
    RoomMemberEvent,
    RoomMessageMedia,
    RoomMessageText,
    RoomEncryptedMedia,
    RoomSendResponse,
    UnknownEvent,
    UploadResponse,
)
from nio.events.room_events import ReactionEvent
from nio.crypto import TrustState

from enclave.common.logging import get_logger

log = get_logger("matrix")

# Type for message handler callbacks
MatrixMessageHandler = Callable[
    [str, str, str, dict[str, Any], list[dict[str, Any]]],
    Awaitable[None],
]
# (room_id, sender, body, event_source, attachments)

# Type for user-join handler callbacks
MatrixJoinHandler = Callable[[str, str], Awaitable[None]]
# (room_id, user_id)

# Type for reaction handler callbacks
MatrixReactionHandler = Callable[[str, str, str, str], Awaitable[None]]
# (room_id, sender, reacts_to_event_id, emoji_key)

# Type for poll response handler callbacks
MatrixPollResponseHandler = Callable[[str, str, str, list[str]], Awaitable[None]]
# (room_id, sender, poll_event_id, selected_answer_ids)


class EnclaveMatrixClient:
    """Matrix client for the Enclave orchestrator.

    Manages E2EE, device trust, room lifecycle, and message routing.
    Persists device keys across restarts.
    """

    def __init__(
        self,
        homeserver: str,
        user_id: str,
        password: str,
        store_path: str,
        device_name: str = "Enclave Bot",
    ):
        self.homeserver = homeserver
        self.user_id = user_id
        self.password = password
        self.device_name = device_name
        self._start_time = 0.0

        store = Path(store_path)
        store.mkdir(parents=True, exist_ok=True)
        self._store_path = store

        # Try to load persisted device ID
        device_id_file = store / "device_id"
        self._device_id_file = device_id_file
        saved_device_id = None
        if device_id_file.exists():
            saved_device_id = device_id_file.read_text().strip()

        config = AsyncClientConfig(
            store_sync_tokens=True,
            encryption_enabled=True,
        )

        self.client = AsyncClient(
            homeserver=self.homeserver,
            user=self.user_id,
            device_id=saved_device_id,
            store_path=str(store),
            config=config,
        )

        # Global send throttle — serialises all room_send calls and
        # enforces a minimum interval between them to stay under the
        # Synapse rate limit (default rc_message: 0.2/s, burst 10).
        self._send_lock = asyncio.Lock()
        self._last_send: float = 0.0
        # Minimum seconds between consecutive room_send calls.
        # Synapse sustains ~1 msg/5s; we aim for headroom.
        self._min_send_interval: float = 1.0
        # When we hit a 429, back off to this interval temporarily
        self._backoff_interval: float = 6.0
        self._rate_limited_until: float = 0.0

        self._message_handlers: list[MatrixMessageHandler] = []
        self._join_handlers: list[MatrixJoinHandler] = []
        self._reaction_handlers: list[MatrixReactionHandler] = []
        self._poll_handlers: list[MatrixPollResponseHandler] = []
        self._syncing = False

        # Deduplication: track recently seen event IDs to prevent
        # double-processing when sync() is called inside callbacks.
        # Uses an ordered dict for proper LRU eviction (oldest first).
        self._seen_events: dict[str, None] = {}
        self._seen_events_max = 1000

        # Per-room event counter for volume-based purge triggers
        self._event_counts: dict[str, int] = {}  # room_id → events sent since reset

        # Register internal callbacks
        self.client.add_event_callback(self._on_message, RoomMessageText)
        self.client.add_event_callback(
            self._on_media, (RoomMessageMedia, RoomEncryptedMedia)
        )
        self.client.add_event_callback(self._on_invite, InviteMemberEvent)
        self.client.add_event_callback(self._on_room_member, RoomMemberEvent)
        self.client.add_event_callback(self._on_reaction, ReactionEvent)
        self.client.add_event_callback(self._on_unknown_event, UnknownEvent)
        self.client.add_event_callback(self._on_megolm, MegolmEvent)
        self.client.add_event_callback(self._on_encrypted, RoomEncryptionEvent)
        # Catch-all for debugging — see all events
        self.client.add_event_callback(self._on_any_event, Event)

    def on_message(self, handler: MatrixMessageHandler) -> None:
        """Register a handler for incoming room messages."""
        self._message_handlers.append(handler)

    def on_user_join(self, handler: MatrixJoinHandler) -> None:
        """Register a handler for when a user joins a room."""
        self._join_handlers.append(handler)

    def on_reaction(self, handler: MatrixReactionHandler) -> None:
        """Register a handler for reaction events."""
        self._reaction_handlers.append(handler)

    def on_poll_response(self, handler: MatrixPollResponseHandler) -> None:
        """Register a handler for poll response events."""
        self._poll_handlers.append(handler)

    async def login(self) -> bool:
        """Login to the homeserver.

        Tries to restore a previous session first to avoid rate limits.
        Falls back to password login if no saved session exists.

        Returns True on success, False on failure.
        """
        access_token_file = self._store_path / "access_token"

        # Try restore_login first (no network request → no rate limit)
        if (
            self._device_id_file.exists()
            and access_token_file.exists()
        ):
            device_id = self._device_id_file.read_text().strip()
            access_token = access_token_file.read_text().strip()
            self.client.restore_login(
                user_id=self.user_id,
                device_id=device_id,
                access_token=access_token,
            )
            log.info(
                "Restored session as %s (device: %s)", self.user_id, device_id
            )
            if self.client.should_upload_keys:
                await self.client.keys_upload()
                log.info("E2EE keys uploaded")
            return True

        # Fresh login
        resp = await self.client.login(self.password, device_name=self.device_name)
        if isinstance(resp, LoginResponse):
            log.info("Logged in as %s (device: %s)", resp.user_id, resp.device_id)
            # Persist for next restart
            self._device_id_file.write_text(resp.device_id)
            access_token_file.write_text(resp.access_token)
            if self.client.should_upload_keys:
                await self.client.keys_upload()
                log.info("E2EE keys uploaded")
            return True
        else:
            log.error("Login failed: %s", resp)
            return False

    async def initial_sync(self) -> None:
        """Perform initial sync and trust devices."""
        self._start_time = time.time()
        await self.client.sync(timeout=10000, full_state=True)
        await self._trust_all_devices()

        # Invalidate all outbound Megolm sessions so the next send creates
        # fresh ones and shares keys with every currently-trusted device.
        # Without this, a restart can leave stale sessions that exclude
        # devices added since the session was first created.
        for room_id in list(self.client.rooms):
            self.client.invalidate_outbound_session(room_id)
        log.info("Invalidated outbound Megolm sessions for %d rooms", len(self.client.rooms))

        # Auto-join any pending invites from before we started syncing
        for room_id in list(self.client.invited_rooms):
            log.info("Pending invite for %s — joining", room_id)
            await self.client.join(room_id)

        log.info("Initial sync complete — %d rooms", len(self.client.rooms))

    async def sync_forever(self, timeout: int = 30000) -> None:
        """Start the sync loop with automatic reconnection.

        On transient errors, retries with exponential backoff (1s → 60s max).
        Resets backoff after a successful sync.
        """
        self._syncing = True
        log.info("Starting sync loop")
        sync_count = 0
        backoff = 1.0
        max_backoff = 60.0

        while self._syncing:
            try:
                resp = await self.client.sync(timeout=timeout)
                sync_count += 1
                backoff = 1.0  # reset on success
                if sync_count <= 3 or sync_count % 100 == 0:
                    log.debug("Sync #%d complete", sync_count)
            except asyncio.CancelledError:
                return
            except Exception as e:
                log.error(
                    "Sync error (retry in %.0fs): %s", backoff, e,
                    exc_info=True,
                )
                try:
                    await asyncio.sleep(backoff)
                except asyncio.CancelledError:
                    return
                backoff = min(backoff * 2, max_backoff)

    def stop_sync(self) -> None:
        """Signal the sync loop to stop."""
        self._syncing = False

    async def _throttled_room_send(
        self,
        room_id: str,
        message_type: str,
        content: dict[str, Any],
    ) -> RoomSendResponse | Any:
        """Send an event through the global rate-limit gate.

        Serialises all room_send calls and enforces a minimum interval
        between them.  When we detect a 429, increases the interval
        temporarily so we drain the backlog without cascading retries.
        """
        async with self._send_lock:
            now = time.monotonic()
            # Use backoff interval if we recently hit a 429
            interval = (
                self._backoff_interval
                if now < self._rate_limited_until
                else self._min_send_interval
            )
            wait = interval - (now - self._last_send)
            if wait > 0:
                await asyncio.sleep(wait)

            resp = await self.client.room_send(
                room_id=room_id,
                message_type=message_type,
                content=content,
            )
            self._last_send = time.monotonic()

            # Detect 429 from nio's transport response (it retries
            # internally but we still see the delay).  If the call
            # took much longer than expected, assume rate-limiting.
            elapsed = self._last_send - (now + max(wait, 0))
            if elapsed > 3.0:
                self._rate_limited_until = self._last_send + 30.0
                log.warning(
                    "Probable rate-limit (send took %.1fs), "
                    "backing off for 30s",
                    elapsed,
                )

            return resp

    async def send_message(
        self,
        room_id: str,
        body: str,
        html_body: str | None = None,
        thread_event_id: str | None = None,
    ) -> str | None:
        """Send a text message to a room.

        Args:
            room_id: Target room ID.
            body: Plain text message body.
            html_body: Optional HTML formatted body.
            thread_event_id: If set, reply in this thread.

        Returns:
            Event ID of sent message, or None on failure.
        """
        # Ensure we have keys for all devices in the room
        await self._ensure_keys_for_room(room_id)

        content: dict[str, Any] = {
            "msgtype": "m.text",
            "body": body,
        }

        if html_body:
            content["format"] = "org.matrix.custom.html"
            content["formatted_body"] = html_body

        if thread_event_id:
            content["m.relates_to"] = {
                "rel_type": "m.thread",
                "event_id": thread_event_id,
                "is_falling_back": True,
                "m.in_reply_to": {"event_id": thread_event_id},
            }

        resp = await self._throttled_room_send(
            room_id=room_id,
            message_type="m.room.message",
            content=content,
        )

        if isinstance(resp, RoomSendResponse):
            log.debug("Sent to %s: %s", room_id, resp.event_id)
            self._event_counts[room_id] = self._event_counts.get(room_id, 0) + 1
            return resp.event_id
        else:
            log.error("Failed to send to %s: %s", room_id, resp)
            return None

    async def _ensure_keys_for_room(self, room_id: str) -> None:
        """Query keys and trust new devices for a room before sending.

        If any untrusted device is found, trust it and invalidate the outbound
        Megolm session so the next room_send creates a fresh one that includes
        the new device.
        """
        try:
            await self.client.keys_query()
        except Exception:
            pass  # "No key query required" is normal

        room = self.client.rooms.get(room_id)
        if not room:
            return

        new_device = False
        for user_id in room.users:
            for device_id, device in self.client.device_store[user_id].items():
                if device.deleted:
                    continue
                if device.trust_state != TrustState.verified:
                    self.client.verify_device(device)
                    log.info("Trusted new device %s for %s", device_id, user_id)
                    new_device = True

        if new_device:
            self.client.invalidate_outbound_session(room_id)
            log.info("Rotated outbound Megolm session for %s (new device)", room_id)

    async def send_reaction(
        self, room_id: str, event_id: str, emoji: str
    ) -> str | None:
        """React to a message with an emoji."""
        await self._ensure_keys_for_room(room_id)
        resp = await self._throttled_room_send(
            room_id=room_id,
            message_type="m.reaction",
            content={
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": event_id,
                    "key": emoji,
                },
            },
        )
        if isinstance(resp, RoomSendResponse):
            return resp.event_id
        return None

    async def send_poll(
        self,
        room_id: str,
        question: str,
        answers: list[tuple[str, str]],
        thread_event_id: str | None = None,
    ) -> str | None:
        """Send a poll to a room.

        Args:
            room_id: Target room ID.
            question: The poll question text.
            answers: List of (answer_id, answer_text) tuples.
            thread_event_id: If set, post in this thread.

        Returns:
            Event ID of the poll event, or None on failure.
        """
        await self._ensure_keys_for_room(room_id)

        poll_answers = [
            {"id": aid, "org.matrix.msc1767.text": text}
            for aid, text in answers
        ]

        content: dict[str, Any] = {
            "org.matrix.msc3381.poll.start": {
                "question": {"org.matrix.msc1767.text": question},
                "kind": "org.matrix.msc3381.poll.disclosed",
                "max_selections": 1,
                "answers": poll_answers,
            },
            "org.matrix.msc1767.text": question,
        }

        if thread_event_id:
            content["m.relates_to"] = {
                "rel_type": "m.thread",
                "event_id": thread_event_id,
                "is_falling_back": True,
                "m.in_reply_to": {"event_id": thread_event_id},
            }

        resp = await self._throttled_room_send(
            room_id=room_id,
            message_type="org.matrix.msc3381.poll.start",
            content=content,
        )

        if isinstance(resp, RoomSendResponse):
            log.debug("Poll sent to %s: %s", room_id, resp.event_id)
            return resp.event_id
        else:
            log.error("Failed to send poll to %s: %s", room_id, resp)
            return None

    async def end_poll(
        self,
        room_id: str,
        poll_event_id: str,
    ) -> str | None:
        """End/close a poll."""
        await self._ensure_keys_for_room(room_id)
        resp = await self._throttled_room_send(
            room_id=room_id,
            message_type="org.matrix.msc3381.poll.end",
            content={
                "m.relates_to": {
                    "rel_type": "m.reference",
                    "event_id": poll_event_id,
                },
                "org.matrix.msc1767.text": "Poll ended",
            },
        )
        if isinstance(resp, RoomSendResponse):
            return resp.event_id
        return None

    async def set_typing(
        self, room_id: str, typing: bool = True, timeout: int = 30000
    ) -> None:
        """Set typing indicator for a room."""
        try:
            await self.client.room_typing(
                room_id, typing_state=typing, timeout=timeout
            )
        except Exception as e:
            log.debug("Typing indicator failed for %s: %s", room_id, e)

    async def invite_user(self, room_id: str, user_id: str) -> bool:
        """Invite a user to a room. Returns True on success."""
        resp = await self.client.room_invite(room_id, user_id)
        if hasattr(resp, "transport_response"):
            # RoomInviteResponse (success)
            log.info("Invited %s to %s", user_id, room_id)
            return True
        log.error("Failed to invite %s to %s: %s", user_id, room_id, resp)
        return False

    async def edit_message(
        self,
        room_id: str,
        event_id: str,
        body: str,
        html_body: str | None = None,
    ) -> str | None:
        """Edit an existing message using m.replace.

        Args:
            room_id: Room containing the message.
            event_id: Event ID of the message to edit.
            body: New plain text body.
            html_body: Optional new HTML body.

        Returns:
            Event ID of the edit event, or None on failure.
        """
        await self._ensure_keys_for_room(room_id)

        new_content: dict[str, Any] = {
            "msgtype": "m.text",
            "body": body,
        }
        if html_body:
            new_content["format"] = "org.matrix.custom.html"
            new_content["formatted_body"] = html_body

        content: dict[str, Any] = {
            "msgtype": "m.text",
            "body": f"* {body}",
            "m.new_content": new_content,
            "m.relates_to": {
                "rel_type": "m.replace",
                "event_id": event_id,
            },
        }

        resp = await self._throttled_room_send(
            room_id=room_id,
            message_type="m.room.message",
            content=content,
        )

        if isinstance(resp, RoomSendResponse):
            self._event_counts[room_id] = self._event_counts.get(room_id, 0) + 1
            return resp.event_id
        else:
            log.error("Failed to edit message %s: %s", event_id, resp)
            return None

    async def redact_event(
        self, room_id: str, event_id: str, reason: str = ""
    ) -> bool:
        """Redact (delete) an event. Useful for removing reactions."""
        try:
            resp = await self.client.room_redact(
                room_id, event_id, reason=reason
            )
            return hasattr(resp, "event_id")
        except Exception as e:
            log.debug("Redact failed for %s: %s", event_id, e)
            return False

    def get_event_count(self, room_id: str) -> int:
        """Return number of events sent to a room since last reset."""
        return self._event_counts.get(room_id, 0)

    def reset_event_count(self, room_id: str) -> None:
        """Reset the event counter for a room (after purge)."""
        self._event_counts.pop(room_id, None)

    async def purge_room_history(
        self, room_id: str, keep_events: int = 200
    ) -> int:
        """Purge old room history via Synapse admin API, keeping last N events.

        Uses the admin room messages endpoint to find the cutoff event,
        then calls purge_history to remove everything older.

        Returns approximate number of events that existed beyond the keep
        window, or -1 on error.
        """
        import aiohttp

        if not self.client.access_token:
            log.error("No access token — cannot purge room history")
            return -1

        base = self.homeserver.rstrip("/")
        headers = {
            "Authorization": f"Bearer {self.client.access_token}",
        }

        try:
            async with aiohttp.ClientSession() as http:
                # Fetch the last `keep_events` messages to find the cutoff
                async with http.get(
                    f"{base}/_synapse/admin/v1/rooms/{room_id}/messages",
                    headers=headers,
                    params={"dir": "b", "limit": str(keep_events)},
                ) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        log.error(
                            "Failed to fetch room messages for purge: %s %s",
                            resp.status, text,
                        )
                        return -1
                    data = await resp.json()

                chunk = data.get("chunk", [])
                if len(chunk) < keep_events:
                    log.debug(
                        "Room %s has %d events (< %d keep) — no purge needed",
                        room_id, len(chunk), keep_events,
                    )
                    return 0

                # The last event in the chunk is the oldest to keep
                cutoff_event_id = chunk[-1]["event_id"]
                log.info(
                    "Purging room %s history before event %s (keeping %d)",
                    room_id, cutoff_event_id, keep_events,
                )

                async with http.post(
                    f"{base}/_synapse/admin/v1/purge_history/{room_id}",
                    headers=headers,
                    json={
                        "purge_up_to_event_id": cutoff_event_id,
                        "delete_local_events": True,
                    },
                ) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        purge_id = result.get("purge_id", "?")
                        log.info(
                            "Room purge started: %s (purge_id=%s)",
                            room_id, purge_id,
                        )
                        return keep_events  # approximate
                    else:
                        text = await resp.text()
                        log.error(
                            "Purge failed for %s: %s %s",
                            room_id, resp.status, text,
                        )
                        return -1
        except Exception as e:
            log.error("Room purge error for %s: %s", room_id, e)
            return -1

    async def create_room(
        self,
        name: str,
        topic: str = "",
        invite: list[str] | None = None,
        encrypted: bool = True,
        space_id: str | None = None,
    ) -> str | None:
        """Create a new Matrix room.

        Args:
            name: Room display name.
            topic: Room topic description.
            invite: List of user IDs to invite.
            encrypted: Enable E2EE (default True).
            space_id: If set, add room to this Space.

        Returns:
            Room ID on success, None on failure.
        """
        initial_state = []
        if encrypted:
            initial_state.append({
                "type": "m.room.encryption",
                "state_key": "",
                "content": {"algorithm": "m.megolm.v1.aes-sha2"},
            })

        resp = await self.client.room_create(
            name=name,
            topic=topic,
            invite=[],
            is_direct=False,
            initial_state=initial_state,
        )

        if isinstance(resp, RoomCreateResponse):
            room_id = resp.room_id
            log.info("Created room '%s': %s", name, room_id)

            # If space_id provided, add room as a child of the space
            if space_id:
                await self._add_room_to_space(space_id, room_id)

            return room_id
        else:
            log.error("Failed to create room '%s': %s", name, resp)
            return None

    async def create_space(self, name: str) -> str | None:
        """Create a Matrix Space.

        Returns:
            Space room ID on success, None on failure.
        """
        resp = await self.client.room_create(
            name=name,
            is_direct=False,
            initial_state=[
                {
                    "type": "m.room.type",
                    "state_key": "",
                    "content": {"type": "m.space"},
                },
            ],
        )
        if isinstance(resp, RoomCreateResponse):
            log.info("Created space '%s': %s", name, resp.room_id)
            return resp.room_id
        else:
            log.error("Failed to create space '%s': %s", name, resp)
            return None

    async def join_room(self, room_id: str) -> bool:
        """Join a room by ID or alias."""
        resp = await self.client.join(room_id)
        return hasattr(resp, "room_id")

    async def kick_user(
        self, room_id: str, user_id: str, reason: str = ""
    ) -> bool:
        """Kick a user from a room."""
        try:
            resp = await self.client.room_kick(room_id, user_id, reason=reason)
            ok = hasattr(resp, "transport_response") or "RoomKickResponse" in type(resp).__name__
            log.info("Kicked %s from %s: %s", user_id, room_id, type(resp).__name__)
            return ok
        except Exception as e:
            log.error("Failed to kick %s from %s: %s", user_id, room_id, e)
            return False

    async def leave_room(self, room_id: str) -> bool:
        """Leave a room."""
        try:
            resp = await self.client.room_leave(room_id)
            ok = hasattr(resp, "room_id") or "RoomLeaveResponse" in type(resp).__name__
            log.info("Left room %s: %s", room_id, type(resp).__name__)
            return ok
        except Exception as e:
            log.error("Failed to leave room %s: %s", room_id, e)
            return False

    async def forget_room(self, room_id: str) -> bool:
        """Forget a room (remove from room list after leaving)."""
        try:
            resp = await self.client.room_forget(room_id)
            ok = hasattr(resp, "room_id") or "RoomForgetResponse" in type(resp).__name__
            log.info("Forgot room %s: %s", room_id, type(resp).__name__)
            return ok
        except Exception as e:
            log.error("Failed to forget room %s: %s", room_id, e)
            return False

    async def cleanup_room(
        self, room_id: str, user_ids: list[str] | None = None, reason: str = ""
    ) -> bool:
        """Full room cleanup: kick users, leave, and forget.

        Args:
            room_id: Room to clean up.
            user_ids: Users to kick before leaving. If None, no kicks.
            reason: Kick/leave reason shown to users.

        Returns:
            True if all operations succeeded.
        """
        ok = True
        if user_ids:
            for uid in user_ids:
                if not await self.kick_user(room_id, uid, reason=reason):
                    ok = False
        if not await self.leave_room(room_id):
            ok = False
        else:
            await self.forget_room(room_id)
        return ok

    async def close(self) -> None:
        """Close the Matrix client."""
        self.stop_sync()
        await self.client.close()
        log.info("Matrix client closed")

    async def _trust_all_devices(self) -> None:
        """Trust all devices in all joined rooms (for E2EE)."""
        try:
            await self.client.keys_query()
        except LocalProtocolError:
            pass
        for room_id in self.client.rooms:
            await self._trust_devices_in_room(room_id)

    async def _trust_devices_in_room(self, room_id: str) -> None:
        """Trust all devices for users in a specific room."""
        room = self.client.rooms.get(room_id)
        if not room:
            return

        for user_id in room.users:
            for device_id, device in self.client.device_store[user_id].items():
                if device.deleted:
                    continue
                if device.trust_state != TrustState.verified:
                    self.client.verify_device(device)
                    log.debug("Trusted device %s for %s", device_id, user_id)

    async def _trust_users(self, user_ids: list[str]) -> None:
        """Query keys and trust all devices for specific users.

        Used after room creation when the room may not be in the sync
        store yet — trusts by user ID rather than room membership.
        """
        try:
            await self.client.keys_query()
        except Exception:
            pass  # "No key query required" is normal

        for user_id in user_ids:
            for device_id, device in self.client.device_store[user_id].items():
                if device.deleted:
                    continue
                if device.trust_state != TrustState.verified:
                    self.client.verify_device(device)
                    log.debug("Trusted device %s for %s", device_id, user_id)

    async def _add_room_to_space(self, space_id: str, room_id: str) -> None:
        """Add a room as a child of a Space."""
        try:
            await self.client.room_put_state(
                room_id=space_id,
                event_type="m.space.child",
                content={"via": [self.homeserver.split("//")[1]]},
                state_key=room_id,
            )
            log.debug("Added %s to space %s", room_id, space_id)
        except Exception as e:
            log.warning("Failed to add room to space: %s", e)

    def _dedup_event(self, event_id: str) -> bool:
        """Return True if this event was already seen (duplicate).

        Uses an ordered dict as an LRU cache so trimming always
        evicts the oldest entries (not arbitrary ones).
        """
        if event_id in self._seen_events:
            return True
        self._seen_events[event_id] = None
        # Trim oldest entries when over capacity
        while len(self._seen_events) > self._seen_events_max:
            # popitem(last=False) removes the oldest (first-inserted) entry
            self._seen_events.pop(next(iter(self._seen_events)))
        return False

    async def _on_message(self, room: MatrixRoom, event: RoomMessageText) -> None:
        """Handle incoming text messages."""
        # Ignore our own messages
        if event.sender == self.client.user_id:
            return

        # Ignore old messages (before we started)
        if event.server_timestamp < (self._start_time * 1000 - 5000):
            return

        # Deduplicate — sync inside callbacks can re-deliver events
        if self._dedup_event(event.event_id):
            log.debug("Duplicate event %s, skipping", event.event_id)
            return

        source = event.source or {}
        for handler in self._message_handlers:
            try:
                await handler(room.room_id, event.sender, event.body, source, [])
            except Exception as e:
                log.error("Message handler error: %s", e)

    async def _on_media(self, room: MatrixRoom, event) -> None:
        """Handle incoming media messages (images, files, audio, video)."""
        if event.sender == self.client.user_id:
            return

        if event.server_timestamp < (self._start_time * 1000 - 5000):
            return

        # Deduplicate
        if self._dedup_event(event.event_id):
            log.debug("Duplicate media event %s, skipping", event.event_id)
            return

        source = event.source or {}
        content = source.get("content", {})
        info = content.get("info", {})
        filename = getattr(event, "body", "attachment")
        url = getattr(event, "url", None) or content.get("url", "")
        mimetype = info.get("mimetype", "")
        size = info.get("size", 0)

        attachment = {
            "filename": filename,
            "url": url,
            "content_type": mimetype,
            "size": size,
        }

        # For encrypted media, store decryption info
        file_info = content.get("file", {})
        if file_info:
            attachment["url"] = file_info.get("url", url)
            attachment["encryption"] = {
                "key": file_info.get("key", {}),
                "iv": file_info.get("iv", ""),
                "hashes": file_info.get("hashes", {}),
            }

        body = f"[Sent a file: {filename}]"
        log.info(
            "Media from %s in %s: %s (%s, %d bytes)",
            event.sender, room.room_id, filename, mimetype, size,
        )

        for handler in self._message_handlers:
            try:
                await handler(
                    room.room_id, event.sender, body, source, [attachment]
                )
            except Exception as e:
                log.error("Message handler error (media): %s", e)

    async def download_media(
        self,
        mxc_url: str,
        dest: str | Path,
        encryption: dict[str, Any] | None = None,
    ) -> bool:
        """Download a file from the Matrix content repository.

        Args:
            mxc_url: The mxc:// URL of the file.
            dest: Local path to save the file to.
            encryption: Optional E2EE decryption info (key, iv, hashes)
                        from the ``file`` field of an encrypted media event.

        Returns:
            True if downloaded successfully.
        """
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)

        try:
            resp = await self.client.download(mxc_url)
            if isinstance(resp, DownloadResponse):
                data = resp.body
                # Decrypt if encryption info was provided (E2EE rooms)
                if encryption:
                    try:
                        from nio.crypto import decrypt_attachment
                        key_obj = encryption.get("key", {})
                        k = key_obj.get("k", "") if isinstance(key_obj, dict) else ""
                        iv = encryption.get("iv", "")
                        hashes = encryption.get("hashes", {})
                        sha256 = hashes.get("sha256", "")
                        if k and iv and sha256:
                            data = decrypt_attachment(data, k, sha256, iv)
                            log.debug("Decrypted attachment: %d bytes", len(data))
                    except Exception as e:
                        log.error("Failed to decrypt attachment: %s", e)
                        return False
                with open(dest, "wb") as f:
                    f.write(data)
                log.info("Downloaded %s → %s (%d bytes)", mxc_url, dest, len(data))
                return True
            else:
                log.error("Download failed for %s: %s", mxc_url, resp)
                return False
        except Exception as e:
            log.error("Download error for %s: %s", mxc_url, e)
            return False

    async def upload_file(
        self, room_id: str, file_path: str | Path, body: str | None = None
    ) -> str | None:
        """Upload a file to Matrix and send it to a room.

        Args:
            room_id: The room to send the file to.
            file_path: Local path of the file to upload.
            body: Optional message body (defaults to filename).

        Returns:
            The event ID if sent, or None on failure.
        """
        file_path = Path(file_path)
        if not file_path.exists():
            log.error("File not found: %s", file_path)
            return None

        mime_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        filename = file_path.name
        file_size = file_path.stat().st_size

        if mime_type.startswith("image/"):
            msgtype = "m.image"
        elif mime_type.startswith("video/"):
            msgtype = "m.video"
        elif mime_type.startswith("audio/"):
            msgtype = "m.audio"
        else:
            msgtype = "m.file"

        try:
            with open(file_path, "rb") as f:
                resp, _keys = await self.client.upload(
                    f, content_type=mime_type,
                    filename=filename, filesize=file_size,
                )

            if not isinstance(resp, UploadResponse):
                log.error("Upload failed for %s: %s", file_path, resp)
                return None

            content = {
                "msgtype": msgtype,
                "body": body or filename,
                "url": resp.content_uri,
                "info": {
                    "mimetype": mime_type,
                    "size": file_size,
                },
            }

            send_resp = await self._throttled_room_send(
                room_id=room_id,
                message_type="m.room.message",
                content=content,
            )
            if isinstance(send_resp, RoomSendResponse):
                log.info("Uploaded %s to %s: %s", filename, room_id, send_resp.event_id)
                return send_resp.event_id
            else:
                log.error("Send failed for uploaded file: %s", send_resp)
                return None
        except Exception as e:
            log.error("Upload error for %s: %s", file_path, e)
            return None

    async def _on_invite(self, room: MatrixRoom, event: InviteMemberEvent) -> None:
        """Auto-accept room invites."""
        if event.state_key == self.client.user_id:
            log.info("Invited to %s, joining...", room.room_id)
            await self.client.join(room.room_id)

    async def _on_room_member(self, room: MatrixRoom, event: RoomMemberEvent) -> None:
        """Detect when a user joins a room."""
        # Only care about joins (not leaves, bans, etc.)
        if event.membership != "join":
            return
        # Ignore our own joins
        if event.state_key == self.client.user_id:
            return
        # Ignore old events
        if event.server_timestamp < (self._start_time * 1000 - 5000):
            return
        log.info("User %s joined %s", event.state_key, room.room_id)
        for handler in self._join_handlers:
            try:
                await handler(room.room_id, event.state_key)
            except Exception as e:
                log.error("Join handler error: %s", e)

    async def _on_reaction(self, room: MatrixRoom, event: ReactionEvent) -> None:
        """Handle reaction events — forward to registered handlers."""
        # Skip own reactions (bot seeding emoji for approval)
        if event.sender == self.client.user_id:
            return
        # Skip old events
        if event.server_timestamp < (self._start_time * 1000 - 5000):
            return
        if self._dedup_event(event.event_id):
            return

        log.debug(
            "Reaction in %s from %s: %s on %s",
            room.room_id, event.sender, event.key, event.reacts_to,
        )
        for handler in self._reaction_handlers:
            try:
                await handler(room.room_id, event.sender, event.reacts_to, event.key)
            except Exception as e:
                log.error("Reaction handler error: %s", e)

    async def _on_unknown_event(self, room: MatrixRoom, event: UnknownEvent) -> None:
        """Handle unknown events — catches poll responses and other MSC events."""
        if event.sender == self.client.user_id:
            return
        if event.server_timestamp < (self._start_time * 1000 - 5000):
            return

        # Check for poll response events
        event_type = getattr(event, "type", "")
        if "poll.response" not in event_type:
            return

        if self._dedup_event(event.event_id):
            return

        source = event.source or {}
        content = source.get("content", {})

        # Extract the poll event ID this responds to
        relates_to = content.get("m.relates_to", {})
        poll_event_id = relates_to.get("event_id", "")
        if not poll_event_id:
            return

        # Extract selected answer IDs
        poll_response = (
            content.get("org.matrix.msc3381.poll.response", {})
        )
        answers = poll_response.get("answers", [])

        log.debug(
            "Poll response in %s from %s: answers=%s for poll %s",
            room.room_id, event.sender, answers, poll_event_id,
        )

        for handler in self._poll_handlers:
            try:
                await handler(room.room_id, event.sender, poll_event_id, answers)
            except Exception as e:
                log.error("Poll response handler error: %s", e)

    async def _on_megolm(self, room: MatrixRoom, event: MegolmEvent) -> None:
        """Log undecryptable messages without replying (to avoid feedback loops)."""
        # Silently ignore old messages and our own
        if event.server_timestamp < (self._start_time * 1000 - 5000):
            return
        if event.sender == self.client.user_id:
            return
        log.warning(
            "Undecryptable message from %s in %s (session: %s)",
            event.sender,
            room.room_id,
            event.session_id,
        )

    async def _on_encrypted(
        self, room: MatrixRoom, event: RoomEncryptionEvent
    ) -> None:
        """Log encrypted events that couldn't be decrypted at all."""
        log.warning(
            "Encrypted event not decrypted in %s from %s: %s",
            room.room_id,
            event.sender,
            type(event).__name__,
        )

    async def _on_any_event(self, room: MatrixRoom, event: Event) -> None:
        """Debug: log all events."""
        etype = type(event).__name__
        sender = getattr(event, "sender", "?")
        if etype not in ("RoomMessageText", "MegolmEvent", "RoomEncryptionEvent"):
            log.debug("Event [%s] %s in %s from %s", etype, getattr(event, "event_id", ""), room.room_id, sender)
