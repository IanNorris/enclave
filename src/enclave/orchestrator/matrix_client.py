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

from nio import (
    AsyncClient,
    AsyncClientConfig,
    Event,
    InviteMemberEvent,
    LoginResponse,
    MatrixRoom,
    MegolmEvent,
    RoomCreateResponse,
    RoomEncryptionEvent,
    RoomMessageText,
    RoomSendResponse,
)
from nio.crypto import TrustState

from enclave.common.logging import get_logger

log = get_logger("matrix")

# Type for message handler callbacks
MatrixMessageHandler = Callable[[str, str, str, dict[str, Any]], Awaitable[None]]
# (room_id, sender, body, event_source)


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

        self._message_handlers: list[MatrixMessageHandler] = []
        self._syncing = False

        # Register internal callbacks
        self.client.add_event_callback(self._on_message, RoomMessageText)
        self.client.add_event_callback(self._on_invite, InviteMemberEvent)
        self.client.add_event_callback(self._on_megolm, MegolmEvent)
        self.client.add_event_callback(self._on_encrypted, RoomEncryptionEvent)

    def on_message(self, handler: MatrixMessageHandler) -> None:
        """Register a handler for incoming room messages."""
        self._message_handlers.append(handler)

    async def login(self) -> bool:
        """Login to the homeserver.

        Returns True on success, False on failure.
        """
        resp = await self.client.login(self.password, device_name=self.device_name)
        if isinstance(resp, LoginResponse):
            log.info("Logged in as %s (device: %s)", resp.user_id, resp.device_id)
            # Persist device ID for reuse across restarts
            self._device_id_file.write_text(resp.device_id)
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
        log.info("Initial sync complete — %d rooms", len(self.client.rooms))

    async def sync_forever(self, timeout: int = 30000) -> None:
        """Start the sync loop. Blocks until stopped."""
        self._syncing = True
        log.info("Starting sync loop")
        try:
            while self._syncing:
                await self.client.sync(timeout=timeout)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.error("Sync error: %s", e)
            raise

    def stop_sync(self) -> None:
        """Signal the sync loop to stop."""
        self._syncing = False

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

        resp = await self.client.room_send(
            room_id=room_id,
            message_type="m.room.message",
            content=content,
        )

        if isinstance(resp, RoomSendResponse):
            return resp.event_id
        else:
            log.error("Failed to send message to %s: %s", room_id, resp)
            return None

    async def _ensure_keys_for_room(self, room_id: str) -> None:
        """Query keys and trust devices for a room before sending."""
        try:
            await self.client.keys_query()
        except Exception:
            pass  # "No key query required" is normal
        try:
            await self._trust_devices_in_room(room_id)
        except Exception as e:
            log.warning("Trust failed for %s: %s", room_id, e)

    async def send_reaction(
        self, room_id: str, event_id: str, emoji: str
    ) -> str | None:
        """React to a message with an emoji."""
        await self._ensure_keys_for_room(room_id)
        resp = await self.client.room_send(
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
            invite=invite or [],
            is_direct=False,
            initial_state=initial_state,
        )

        if isinstance(resp, RoomCreateResponse):
            room_id = resp.room_id
            log.info("Created room '%s': %s", name, room_id)

            # If space_id provided, add room as a child of the space
            if space_id:
                await self._add_room_to_space(space_id, room_id)

            # Sync to pick up the new room, then trust devices
            await self.client.sync(timeout=5000)
            await self._trust_devices_in_room(room_id)

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

    async def close(self) -> None:
        """Close the Matrix client."""
        self.stop_sync()
        await self.client.close()
        log.info("Matrix client closed")

    async def _trust_all_devices(self) -> None:
        """Trust all devices in all joined rooms (for E2EE)."""
        await self.client.keys_query()
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

    async def _on_message(self, room: MatrixRoom, event: RoomMessageText) -> None:
        """Handle incoming text messages."""
        # Ignore our own messages
        if event.sender == self.client.user_id:
            return

        # Ignore old messages (before we started)
        if event.server_timestamp < (self._start_time * 1000 - 5000):
            return

        source = event.source or {}
        for handler in self._message_handlers:
            try:
                await handler(room.room_id, event.sender, event.body, source)
            except Exception as e:
                log.error("Message handler error: %s", e)

    async def _on_invite(self, room: MatrixRoom, event: InviteMemberEvent) -> None:
        """Auto-accept room invites."""
        if event.state_key == self.client.user_id:
            log.info("Invited to %s, joining...", room.room_id)
            await self.client.join(room.room_id)

    async def _on_megolm(self, room: MatrixRoom, event: MegolmEvent) -> None:
        """Log undecryptable messages."""
        log.debug(
            "Undecryptable message from %s (session: %s)",
            event.sender,
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
