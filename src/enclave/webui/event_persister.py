"""Always-on event persistence for the Web UI.

The orchestrator's control socket is *live-only*: it pushes events to whatever
subscribers are currently connected and keeps no replay buffer. Historically the
Web UI only wrote events to ``events.db`` while a browser WebSocket happened to
be connected, so any agent activity that occurred while no tab was open was lost
forever — messages "disappeared" after a reload.

``EventPersister`` fixes that structurally: it runs for the entire lifetime of
the webui process, subscribes to every running session, and durably persists all
meaningful events to each session's ``events.db`` regardless of whether any
browser is connected. It is the single writer to the event store; the WebSocket
handler only relays events to the browser for live display.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from enclave.webui.event_store import PERSIST_TYPES, persist_event

log = logging.getLogger(__name__)

# How often to re-scan the running-session list (and respawn dropped subscriptions).
_SCAN_INTERVAL = 5.0


class EventPersister:
    """Background task that durably persists agent events for all sessions."""

    def __init__(self, sock_path: Path, workspace_base: Path):
        self._sock_path = sock_path
        self._workspace_base = workspace_base
        # session_id → live subscription task
        self._tasks: dict[str, asyncio.Task] = {}

    async def run(self) -> None:
        """Main loop: keep one live subscription per running session."""
        while True:
            try:
                await self._tick()
            except Exception:  # never let the persister die
                log.exception("EventPersister tick failed")
            await asyncio.sleep(_SCAN_INTERVAL)

    async def _tick(self) -> None:
        # Reap finished subscription tasks (session stopped or stream dropped).
        for sid in [s for s, t in self._tasks.items() if t.done()]:
            self._tasks.pop(sid, None)

        if not self._sock_path.exists():
            return

        running = await self._list_running_sessions()
        # Spawn a subscriber for any running session that lacks a live one.
        for sid in running:
            if sid not in self._tasks:
                self._tasks[sid] = asyncio.create_task(self._subscribe(sid))

    async def _list_running_sessions(self) -> list[str]:
        try:
            reader, writer = await asyncio.open_unix_connection(str(self._sock_path))
            writer.write(json.dumps({"action": "list"}).encode() + b"\n")
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            writer.close()
            await writer.wait_closed()
            data = json.loads(line.decode())
            return [
                s["id"]
                for s in data.get("sessions", [])
                if s.get("status") == "running" and s.get("id")
            ]
        except (OSError, ConnectionError, json.JSONDecodeError, asyncio.TimeoutError):
            return []

    async def _subscribe(self, session_id: str) -> None:
        """Subscribe to one session and persist every relevant event.

        Returns when the stream drops; the main loop respawns it. Because the
        orchestrator never replays, a reconnect cannot duplicate events.
        """
        writer = None
        try:
            reader, writer = await asyncio.open_unix_connection(str(self._sock_path))
            writer.write(
                json.dumps({"action": "subscribe", "session": session_id}).encode() + b"\n"
            )
            await writer.drain()

            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    event = json.loads(line.decode())
                except json.JSONDecodeError:
                    continue
                if event.get("type") not in PERSIST_TYPES:
                    continue
                persist_event(self._workspace_base, session_id, event)
        except (OSError, ConnectionError, asyncio.CancelledError):
            pass
        finally:
            if writer is not None:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass
