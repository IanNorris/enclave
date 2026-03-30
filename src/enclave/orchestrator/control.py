"""Control socket for injecting messages into agent sessions.

Allows external tools (e.g. the host Copilot CLI) to send messages
directly to running agents and receive streamed responses.

Protocol: newline-delimited JSON over a Unix domain socket.

Request:
    {"action": "send", "session": "<session_id>", "content": "...", "sender": "..."}
    {"action": "list"}

Response (streamed, one JSON object per line):
    {"ok": true, "type": "ack"}
    {"ok": true, "type": "response", "content": "..."}
    {"ok": true, "type": "turn_end"}
    {"ok": false, "error": "..."}
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from enclave.common.protocol import Message, MessageType

if TYPE_CHECKING:
    from .router import MessageRouter

log = logging.getLogger("enclave.control")


class ControlServer:
    """Unix socket server for external control of agent sessions."""

    def __init__(self, socket_path: str | Path, router: MessageRouter):
        self._socket_path = Path(socket_path)
        self._router = router
        self._server: asyncio.AbstractServer | None = None
        # session_id → set of (queue) for response subscribers
        self._subscribers: dict[str, set[asyncio.Queue[dict]]] = {}
        # Debounce timers for turn_end (cancel if turn_start follows)
        self._turn_end_timers: dict[str, asyncio.TimerHandle] = {}

    async def start(self) -> None:
        self._socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self._socket_path.exists():
            self._socket_path.unlink()
        self._server = await asyncio.start_unix_server(
            self._handle_client, path=str(self._socket_path)
        )
        os.chmod(str(self._socket_path), 0o660)
        log.info("Control socket listening: %s", self._socket_path)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        if self._socket_path.exists():
            self._socket_path.unlink(missing_ok=True)

    def notify_response(self, session_id: str, content: str) -> None:
        """Called by the router when an agent sends a response."""
        for q in self._subscribers.get(session_id, set()):
            q.put_nowait({"ok": True, "type": "response", "content": content})

    def notify_turn_end(self, session_id: str) -> None:
        """Called by the router when an agent's turn ends.

        Debounced: waits 2s before notifying subscribers, cancelled if a new
        turn starts (agents do multiple turns per interaction).
        """
        # Cancel any pending debounce
        timer = self._turn_end_timers.pop(session_id, None)
        if timer:
            timer.cancel()

        subs = self._subscribers.get(session_id)
        if not subs:
            return

        def _fire() -> None:
            self._turn_end_timers.pop(session_id, None)
            for q in self._subscribers.get(session_id, set()):
                q.put_nowait({"ok": True, "type": "turn_end"})

        loop = asyncio.get_event_loop()
        self._turn_end_timers[session_id] = loop.call_later(2.0, _fire)

    def cancel_turn_end(self, session_id: str) -> None:
        """Cancel a pending turn_end debounce (called on turn_start)."""
        timer = self._turn_end_timers.pop(session_id, None)
        if timer:
            timer.cancel()

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=10.0)
            if not raw:
                return
            req = json.loads(raw.decode())
            action = req.get("action")

            if action == "list":
                await self._handle_list(writer)
            elif action == "send":
                await self._handle_send(req, writer, reader)
            elif action == "stop":
                await self._handle_stop(req, writer)
            else:
                await self._write(writer, {"ok": False, "error": f"Unknown action: {action}"})
        except asyncio.TimeoutError:
            await self._write(writer, {"ok": False, "error": "Timeout reading request"})
        except json.JSONDecodeError as e:
            await self._write(writer, {"ok": False, "error": f"Invalid JSON: {e}"})
        except Exception as e:
            log.warning("Control client error: %s", e)
            try:
                await self._write(writer, {"ok": False, "error": str(e)})
            except Exception:
                pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _handle_list(self, writer: asyncio.StreamWriter) -> None:
        sessions = []
        for session in self._router.containers.list_sessions():
            sessions.append({
                "id": session.id,
                "name": session.name,
                "status": session.status,
                "room_id": session.room_id,
            })
        await self._write(writer, {"ok": True, "type": "sessions", "sessions": sessions})

    async def _handle_stop(self, req: dict, writer: asyncio.StreamWriter) -> None:
        session_id = req.get("session", "")
        if not session_id:
            await self._write(writer, {"ok": False, "error": "Missing session"})
            return

        session = self._router.containers.get_session(session_id)
        if not session:
            await self._write(writer, {"ok": False, "error": f"Session not found: {session_id}"})
            return

        # Send shutdown message to agent, then stop container
        await self._router.ipc.send_to(
            session_id,
            Message(type=MessageType.SHUTDOWN, payload={}),
        )
        ok = await self._router.containers.stop_session(session_id)
        await self._router.ipc.remove_socket(session_id)

        if ok:
            await self._write(writer, {"ok": True, "type": "stopped"})
        else:
            await self._write(writer, {"ok": False, "error": "Failed to stop session"})

    async def _handle_send(
        self,
        req: dict,
        writer: asyncio.StreamWriter,
        reader: asyncio.StreamReader,
    ) -> None:
        session_id = req.get("session", "")
        content = req.get("content", "")
        sender = req.get("sender", "[Orchestrator]")

        if not session_id or not content:
            await self._write(writer, {"ok": False, "error": "Missing session or content"})
            return

        session = self._router.containers.get_session(session_id)
        if not session:
            await self._write(writer, {"ok": False, "error": f"Session not found: {session_id}"})
            return

        # Subscribe to responses before sending
        q: asyncio.Queue[dict] = asyncio.Queue()
        subs = self._subscribers.setdefault(session_id, set())
        subs.add(q)

        try:
            # Tag the message so the agent knows it's from the orchestrator
            tagged = f"[{sender}] {content}"
            ok = await self._router.inject_message(session_id, tagged)
            if not ok:
                await self._write(writer, {"ok": False, "error": "Failed to send to agent"})
                return

            await self._write(writer, {"ok": True, "type": "ack"})

            # Stream responses until turn_end or timeout
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=300.0)
                    await self._write(writer, msg)
                    if msg.get("type") == "turn_end":
                        break
                except asyncio.TimeoutError:
                    await self._write(writer, {"ok": False, "error": "Response timeout"})
                    break
        finally:
            subs.discard(q)
            if not subs:
                self._subscribers.pop(session_id, None)

    @staticmethod
    async def _write(writer: asyncio.StreamWriter, data: dict) -> None:
        writer.write(json.dumps(data).encode() + b"\n")
        await writer.drain()
