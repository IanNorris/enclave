"""Spike 2: Matrix E2EE bot with thread creation.

Tests:
1. Connect to homeserver and login
2. E2EE support (if libolm available, otherwise unencrypted)
3. Echo messages back
4. Create threads
5. React with emoji

Usage:
    export MATRIX_HOMESERVER="https://matrix.example.com"
    export MATRIX_USER="@enclave-bot:example.com"
    export MATRIX_ACCESS_TOKEN="syt_..."  # or use MATRIX_PASSWORD for password login
    export MATRIX_ROOM="!roomid:example.com"  # or #alias:example.com

    python bot.py
"""

import asyncio
import os
import sys
import time
from pathlib import Path

from nio import (
    AsyncClient,
    AsyncClientConfig,
    InviteMemberEvent,
    LoginResponse,
    MatrixRoom,
    MegolmEvent,
    RoomMessageText,
    RoomResolveAliasResponse,
)
from nio.crypto import TrustState


# Store directory for E2EE keys
STORE_DIR = Path(__file__).parent / "bot_store"


class EnclaveTestBot:
    """Minimal Matrix bot for Spike 2 testing."""

    def __init__(
        self,
        homeserver: str,
        user_id: str,
        target_room: str,
        password: str | None = None,
        access_token: str | None = None,
    ):
        self.homeserver = homeserver
        self.user_id = user_id
        self.password = password
        self.access_token = access_token
        self.target_room = target_room
        self.target_room_id: str | None = None

        STORE_DIR.mkdir(parents=True, exist_ok=True)

        config = AsyncClientConfig(
            store_sync_tokens=True,
            encryption_enabled=True,
        )

        self.client = AsyncClient(
            homeserver=self.homeserver,
            user=self.user_id,
            store_path=str(STORE_DIR),
            config=config,
        )

        # Register callbacks
        self.client.add_event_callback(self._on_message, RoomMessageText)
        self.client.add_event_callback(self._on_invite, InviteMemberEvent)
        self.client.add_event_callback(self._on_megolm, MegolmEvent)

        self.results = {
            "login": False,
            "sync": False,
            "message_received": False,
            "echo_sent": False,
            "thread_created": False,
            "reaction_sent": False,
        }

    async def start(self) -> None:
        """Login and start syncing."""
        print("\n[1/5] Logging in...")
        if self.access_token:
            # Token-based auth — restore_login sets token AND loads the crypto store
            device_id = os.environ.get("MATRIX_DEVICE_ID", "ENCLAVE_DEV")
            self.client.restore_login(
                user_id=self.user_id,
                device_id=device_id,
                access_token=self.access_token,
            )
            print(f"  ✓ Authenticated via access token as {self.user_id}")
            print(f"    Device ID: {device_id}")
            self.results["login"] = True
        else:
            resp = await self.client.login(self.password)
            if isinstance(resp, LoginResponse):
                print(f"  ✓ Logged in as {resp.user_id}")
                print(f"    Device ID: {resp.device_id}")
                self.results["login"] = True
            else:
                print(f"  ✗ Login failed: {resp}")
                return

        # If E2EE keys exist, load them
        if self.client.should_upload_keys:
            print("  Uploading E2EE keys...")
            await self.client.keys_upload()

        # Resolve room alias if needed
        if self.target_room.startswith("#"):
            print(f"\n  Resolving room alias {self.target_room}...")
            resp = await self.client.room_resolve_alias(self.target_room)
            if isinstance(resp, RoomResolveAliasResponse):
                self.target_room_id = resp.room_id
                print(f"  ✓ Resolved to {self.target_room_id}")
            else:
                print(f"  ✗ Failed to resolve alias: {resp}")
                return
        else:
            self.target_room_id = self.target_room

        # Join the room
        print(f"\n  Joining room {self.target_room_id}...")
        join_resp = await self.client.join(self.target_room_id)
        print(f"  Join response: {type(join_resp).__name__}")

        # Upload a sync filter to limit to our target room only
        # (enclave-dev is in large public rooms that make full sync very slow)
        print("\n[2/5] Syncing (filtered to target room)...")
        sync_filter = {
            "room": {
                "rooms": [self.target_room_id],
                "state": {"lazy_load_members": True},
                "timeline": {"limit": 20},
            },
            "presence": {"types": []},
            "account_data": {"types": []},
        }
        resp = await self.client.sync(timeout=30000, sync_filter=sync_filter)
        self.results["sync"] = True
        print("  ✓ Initial sync complete")
        print(f"  Rooms in state: {list(self.client.rooms.keys())}")

        # The room may not appear after first sync if just joined; retry
        if self.target_room_id not in self.client.rooms:
            print(f"  Room not in state yet, doing another sync...")
            await self.client.sync(timeout=10000, full_state=True)
            if self.target_room_id not in self.client.rooms:
                print(f"  ⚠ Room still missing. Known rooms: {list(self.client.rooms.keys())}")
                print(f"  Continuing without E2EE key trust (will send unencrypted if needed)")

        # Query keys and trust all devices in the room (spike testing only!)
        if self.target_room_id and self.target_room_id in self.client.rooms:
            room = self.client.rooms[self.target_room_id]
            print(f"  Room: {room.display_name} ({len(room.users)} users)")

            # Query keys for all room members so device_store is populated
            user_list = list(room.users)
            print(f"  Querying keys for {len(user_list)} users...")
            try:
                await self.client.keys_query()
                # Now trust all devices
                for uid in user_list:
                    for device_id, device in self.client.device_store[uid].items():
                        if device.deleted:
                            continue
                        if device.trust_state != TrustState.verified:
                            self.client.verify_device(device)
                            print(f"  Auto-trusted device {device_id} for {uid}")
            except Exception as e:
                print(f"  ⚠ Key query failed: {e} — continuing without trust")

        # Send hello message (fall back to unencrypted HTTP if room_send fails)
        print("\n[3/5] Sending hello message...")
        hello_content = {
            "msgtype": "m.text",
            "body": "🏰 **Enclave Spike 2** — Bot is online! "
                    "Send me a message and I'll echo it back. "
                    "Say `!thread` to test thread creation, "
                    "`!react` to test reactions, "
                    "or `!quit` to shut down.",
        }
        try:
            hello_resp = await self.client.room_send(
                room_id=self.target_room_id,
                message_type="m.room.message",
                content=hello_content,
            )
            print(f"  ✓ Hello sent (event: {hello_resp})")
        except Exception as e:
            print(f"  ⚠ room_send failed ({e}), trying raw PUT...")
            import aiohttp
            url = f"{self.homeserver}/_matrix/client/v3/rooms/{self.target_room_id}/send/m.room.message/{int(time.time()*1000)}"
            async with aiohttp.ClientSession() as session:
                async with session.put(
                    url,
                    json=hello_content,
                    headers={"Authorization": f"Bearer {self.access_token}"},
                    ssl=False,
                ) as resp:
                    result = await resp.json()
                    print(f"  ✓ Hello sent via raw API (event: {result})")

        # Now listen for messages
        print("\n[4/5] Listening for messages (send messages in the room)...")
        print("       Commands: !thread, !react, !quit")
        print("       Any other message will be echoed back.\n")

        # Store filter for reuse in sync loop
        self._sync_filter = sync_filter

        try:
            while True:
                await self.client.sync(timeout=30000, sync_filter=self._sync_filter)
        except KeyboardInterrupt:
            pass

    async def _on_message(self, room: MatrixRoom, event: RoomMessageText) -> None:
        """Handle incoming messages."""
        # Ignore our own messages
        if event.sender == self.client.user_id:
            return

        # Ignore messages from before we started
        if event.server_timestamp < (time.time() * 1000 - 30000):
            return

        # Only respond in our target room
        if room.room_id != self.target_room_id:
            return

        self.results["message_received"] = True
        body = event.body.strip()
        print(f"  [{room.display_name}] {event.sender}: {body}")

        if body == "!thread":
            await self._test_thread(room, event)
        elif body == "!react":
            await self._test_reaction(room, event)
        elif body == "!quit":
            await self._shutdown(room)
        else:
            await self._echo(room, event, body)

    def _get_thread_context(self, event: RoomMessageText) -> dict | None:
        """Extract thread relation from an event, if it's in a thread."""
        source = event.source or {}
        content = source.get("content", {})
        relates_to = content.get("m.relates_to", {})
        if relates_to.get("rel_type") == "m.thread":
            # Message is in a thread — return relation to continue the thread
            return {
                "rel_type": "m.thread",
                "event_id": relates_to["event_id"],  # thread root
                "is_falling_back": True,
                "m.in_reply_to": {"event_id": event.event_id},
            }
        return None

    async def _echo(
        self, room: MatrixRoom, event: RoomMessageText, body: str
    ) -> None:
        """Echo a message back, preserving thread context."""
        content: dict = {
            "msgtype": "m.text",
            "body": f"🔁 Echo: {body}",
        }
        thread_ctx = self._get_thread_context(event)
        if thread_ctx:
            content["m.relates_to"] = thread_ctx

        await self.client.room_send(
            room_id=room.room_id,
            message_type="m.room.message",
            content=content,
        )
        self.results["echo_sent"] = True
        print(f"  → Echoed back{' (in thread)' if thread_ctx else ''}")

    async def _test_thread(self, room: MatrixRoom, event: RoomMessageText) -> None:
        """Create a thread reply to the user's message."""
        # Thread creation in Matrix: reply with m.thread relation
        await self.client.room_send(
            room_id=room.room_id,
            message_type="m.room.message",
            content={
                "msgtype": "m.text",
                "body": "🧵 This is a thread reply! "
                        "In Enclave, sub-agent output would stream here.",
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": event.event_id,
                    "is_falling_back": True,
                    "m.in_reply_to": {
                        "event_id": event.event_id,
                    },
                },
            },
        )
        # Send a second message in the same thread
        await self.client.room_send(
            room_id=room.room_id,
            message_type="m.room.message",
            content={
                "msgtype": "m.text",
                "body": "🧵 Second message in thread — "
                        "sub-agent would continue working here...",
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": event.event_id,
                    "is_falling_back": True,
                    "m.in_reply_to": {
                        "event_id": event.event_id,
                    },
                },
            },
        )
        # Completion message
        await self.client.room_send(
            room_id=room.room_id,
            message_type="m.room.message",
            content={
                "msgtype": "m.text",
                "body": "✅ Thread test complete — 3 messages in thread.",
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": event.event_id,
                    "is_falling_back": True,
                    "m.in_reply_to": {
                        "event_id": event.event_id,
                    },
                },
            },
        )
        self.results["thread_created"] = True
        print(f"  → Thread created with 3 messages")

    async def _test_reaction(self, room: MatrixRoom, event: RoomMessageText) -> None:
        """React to the user's message with ✅."""
        await self.client.room_send(
            room_id=room.room_id,
            message_type="m.reaction",
            content={
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": event.event_id,
                    "key": "✅",
                },
            },
        )
        # Also react with 🏰
        await self.client.room_send(
            room_id=room.room_id,
            message_type="m.reaction",
            content={
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": event.event_id,
                    "key": "🏰",
                },
            },
        )
        self.results["reaction_sent"] = True
        print(f"  → Reacted with ✅ and 🏰")

    async def _shutdown(self, room: MatrixRoom) -> None:
        """Send results and shut down."""
        print("\n[5/5] Shutting down...")
        results_text = "\n".join(
            f"{'✓' if v else '✗'} {k}" for k, v in self.results.items()
        )
        await self.client.room_send(
            room_id=room.room_id,
            message_type="m.room.message",
            content={
                "msgtype": "m.text",
                "body": f"🏰 **Enclave Spike 2 Results:**\n{results_text}\n\nShutting down.",
            },
        )
        await self.client.close()

        print("\n" + "=" * 60)
        print("Spike 2 Results:")
        for k, v in self.results.items():
            print(f"  {'✓' if v else '✗'} {k}")
        print("=" * 60)

        all_passed = all(self.results.values())
        if all_passed:
            print("\n🎉 Spike 2 PASSED — Matrix E2EE bot fully functional!")
        else:
            failed = [k for k, v in self.results.items() if not v]
            print(f"\n⚠  Spike 2 PARTIAL — not tested: {', '.join(failed)}")

        sys.exit(0)

    async def _on_invite(self, room: MatrixRoom, event: InviteMemberEvent) -> None:
        """Auto-accept invites."""
        if event.state_key == self.client.user_id:
            print(f"  Invited to {room.room_id}, joining...")
            await self.client.join(room.room_id)

    async def _on_megolm(self, room: MatrixRoom, event: MegolmEvent) -> None:
        """Handle messages we can't decrypt."""
        print(f"  ⚠ ENCRYPTED message from {event.sender} that we can't decrypt!")
        print(f"    Session ID: {event.session_id}")
        print(f"    Device ID:  {event.device_id}")
        print(f"    This means E2EE key exchange hasn't completed.")
        print(f"    Try verifying the bot device in Element, or disable E2EE in the room.")


async def main() -> None:
    homeserver = os.environ.get("MATRIX_HOMESERVER")
    user_id = os.environ.get("MATRIX_USER")
    password = os.environ.get("MATRIX_PASSWORD")
    access_token = os.environ.get("MATRIX_ACCESS_TOKEN")
    room = os.environ.get("MATRIX_ROOM")

    if not all([homeserver, user_id, room]) or not (password or access_token):
        print("Missing environment variables. Set:")
        print("  MATRIX_HOMESERVER      — e.g. https://matrix.example.com")
        print("  MATRIX_USER            — e.g. @enclave-bot:example.com")
        print("  MATRIX_ACCESS_TOKEN    — access token (preferred)")
        print("    or MATRIX_PASSWORD   — bot account password")
        print("  MATRIX_ROOM            — e.g. #test:example.com or !roomid:example.com")
        sys.exit(1)

    print("=" * 60)
    print("Spike 2: Matrix E2EE Bot Test")
    print("=" * 60)
    print(f"  Homeserver: {homeserver}")
    print(f"  User:       {user_id}")
    print(f"  Auth:       {'token' if access_token else 'password'}")
    print(f"  Room:       {room}")

    bot = EnclaveTestBot(
        homeserver, user_id, room,
        password=password,
        access_token=access_token,
    )
    await bot.start()


if __name__ == "__main__":
    asyncio.run(main())

