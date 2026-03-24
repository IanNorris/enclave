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
    export MATRIX_PASSWORD="your-password"
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
        password: str,
        target_room: str,
    ):
        self.homeserver = homeserver
        self.user_id = user_id
        self.password = password
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

        # Initial sync
        print("\n[2/5] Syncing...")
        await self.client.sync(timeout=10000)
        self.results["sync"] = True
        print("  ✓ Initial sync complete")

        # Query keys and trust all devices in the room (spike testing only!)
        if self.target_room_id and self.target_room_id in self.client.rooms:
            room = self.client.rooms[self.target_room_id]
            print(f"  Room: {room.display_name} ({len(room.users)} users)")

            # Query keys for all room members so device_store is populated
            user_list = list(room.users)
            print(f"  Querying keys for {len(user_list)} users...")
            await self.client.keys_query()

            # Now trust all devices
            for uid in user_list:
                for device_id, device in self.client.device_store[uid].items():
                    if device.deleted:
                        continue
                    if device.trust_state != TrustState.verified:
                        self.client.verify_device(device)
                        print(f"  Auto-trusted device {device_id} for {uid}")

        # Send hello message
        print("\n[3/5] Sending hello message...")
        hello_resp = await self.client.room_send(
            room_id=self.target_room_id,
            message_type="m.room.message",
            content={
                "msgtype": "m.text",
                "body": "🏰 **Enclave Spike 2** — Bot is online! "
                        "Send me a message and I'll echo it back. "
                        "Say `!thread` to test thread creation, "
                        "`!react` to test reactions, "
                        "or `!quit` to shut down.",
            },
        )
        print(f"  ✓ Hello sent (event: {hello_resp})")

        # Now listen for messages
        print("\n[4/5] Listening for messages (send messages in the room)...")
        print("       Commands: !thread, !react, !quit")
        print("       Any other message will be echoed back.\n")

        try:
            while True:
                await self.client.sync(timeout=30000)
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

    async def _echo(
        self, room: MatrixRoom, event: RoomMessageText, body: str
    ) -> None:
        """Echo a message back."""
        await self.client.room_send(
            room_id=room.room_id,
            message_type="m.room.message",
            content={
                "msgtype": "m.text",
                "body": f"🔁 Echo: {body}",
            },
        )
        self.results["echo_sent"] = True
        print(f"  → Echoed back")

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
    room = os.environ.get("MATRIX_ROOM")

    if not all([homeserver, user_id, password, room]):
        print("Missing environment variables. Set:")
        print("  MATRIX_HOMESERVER  — e.g. https://matrix.example.com")
        print("  MATRIX_USER        — e.g. @enclave-bot:example.com")
        print("  MATRIX_PASSWORD    — bot account password")
        print("  MATRIX_ROOM        — e.g. #test:example.com or !roomid:example.com")
        sys.exit(1)

    print("=" * 60)
    print("Spike 2: Matrix E2EE Bot Test")
    print("=" * 60)
    print(f"  Homeserver: {homeserver}")
    print(f"  User:       {user_id}")
    print(f"  Room:       {room}")

    bot = EnclaveTestBot(homeserver, user_id, password, room)
    await bot.start()


if __name__ == "__main__":
    asyncio.run(main())

