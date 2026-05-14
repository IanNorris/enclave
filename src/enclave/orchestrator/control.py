"""Control socket for injecting messages into agent sessions.

Allows external tools (e.g. the host Copilot CLI, web UI) to send messages
directly to running agents and receive streamed responses, or passively
subscribe to a session's event stream.

Protocol: newline-delimited JSON over a Unix domain socket.

Request:
    {"action": "send", "session": "<session_id>", "content": "...", "sender": "..."}
    {"action": "subscribe", "session": "<session_id>"}
    {"action": "list"}

Response (streamed, one JSON object per line):
    {"ok": true, "type": "ack"}
    {"ok": true, "type": "delta", "content": "..."}
    {"ok": true, "type": "thinking", "content": "...", "phase": "start|delta|end"}
    {"ok": true, "type": "tool_start", "name": "...", "detail": "..."}
    {"ok": true, "type": "tool_complete", "name": "...", "success": true}
    {"ok": true, "type": "activity", "text": "..."}
    {"ok": true, "type": "response", "content": "..."}
    {"ok": true, "type": "turn_start"}
    {"ok": true, "type": "turn_end"}
    {"ok": false, "error": "..."}
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

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
        self._emit(session_id, {"ok": True, "type": "response", "content": content})

    def notify_delta(self, session_id: str, content: str) -> None:
        """Called by the router on streaming text delta."""
        self._emit(session_id, {"ok": True, "type": "delta", "content": content})

    def notify_thinking(self, session_id: str, content: str, phase: str = "delta") -> None:
        """Called by the router on thinking/reasoning updates.

        phase: "start" (new thinking block), "delta" (streaming update), "end" (finalized)
        """
        self._emit(session_id, {"ok": True, "type": "thinking", "content": content, "phase": phase})

    def notify_tool_start(self, session_id: str, name: str, detail: str = "") -> None:
        """Called by the router when a tool starts executing."""
        self._emit(session_id, {"ok": True, "type": "tool_start", "name": name, "detail": detail})

    def notify_tool_complete(self, session_id: str, name: str, success: bool = True) -> None:
        """Called by the router when a tool finishes."""
        self._emit(session_id, {"ok": True, "type": "tool_complete", "name": name, "success": success})

    def notify_activity(self, session_id: str, text: str) -> None:
        """Called by the router for activity/status updates."""
        self._emit(session_id, {"ok": True, "type": "activity", "text": text})

    def notify_file_send(
        self, session_id: str, filename: str, mimetype: str = "",
        mxc_url: str = "", event_id: str = "",
    ) -> None:
        """Called by the router when an agent uploads a file."""
        self._emit(session_id, {
            "ok": True, "type": "file_send",
            "filename": filename,
            "mimetype": mimetype,
            "mxc_url": mxc_url,
            "event_id": event_id,
        })

    def notify_ask_user(self, session_id: str, question: str, choices: list[str] | None = None) -> None:
        """Called by the router when the agent asks the user a question."""
        self._emit(session_id, {
            "ok": True, "type": "ask_user",
            "question": question,
            "choices": choices or [],
        })

    def notify_turn_start(self, session_id: str) -> None:
        """Called by the router when an agent turn begins."""
        self._emit(session_id, {"ok": True, "type": "turn_start"})

    def _emit(self, session_id: str, event: dict) -> None:
        """Push an event to all subscribers of a session."""
        for q in self._subscribers.get(session_id, set()):
            q.put_nowait(event)

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
            self._emit(session_id, {"ok": True, "type": "turn_end"})

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
            log.info("Control socket request: action=%s session=%s",
                     action, req.get("session", "-"))

            if action == "list":
                await self._handle_list(writer)
            elif action == "send":
                await self._handle_send(req, writer, reader)
            elif action == "subscribe":
                await self._handle_subscribe(req, writer, reader)
            elif action == "stop":
                await self._handle_stop(req, writer)
            elif action == "start":
                await self._handle_start(req, writer)
            elif action == "delete":
                await self._handle_delete(req, writer)
            elif action == "models":
                await self._handle_models(req, writer)
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

        session = self._router.sessions.get_session(session_id)
        if not session:
            await self._write(writer, {"ok": False, "error": f"Session not found: {session_id}"})
            return

        log.info("Stop requested via control socket: %s", session_id)
        ok = await self._router.sessions.stop_session(session_id, reason="control")

        if ok:
            await self._write(writer, {"ok": True, "type": "stopped"})
        else:
            await self._write(writer, {"ok": False, "error": "Failed to stop session"})

    async def _handle_start(self, req: dict, writer: asyncio.StreamWriter) -> None:
        session_id = req.get("session", "")
        if not session_id:
            await self._write(writer, {"ok": False, "error": "Missing session"})
            return

        session = self._router.sessions.get_session(session_id)
        if not session:
            await self._write(writer, {"ok": False, "error": f"Session not found: {session_id}"})
            return

        if session.status == "running":
            await self._write(writer, {"ok": True, "type": "already_running"})
            return

        log.info("Start requested via control socket: %s", session_id)
        ok, error = await self._router.sessions.restore_session(session_id)

        if ok:
            await self._write(writer, {"ok": True, "type": "started"})
        else:
            await self._write(writer, {"ok": False, "error": error or "Failed to start session"})

    async def _handle_delete(self, req: dict, writer: asyncio.StreamWriter) -> None:
        session_id = req.get("session", "")
        if not session_id:
            await self._write(writer, {"ok": False, "error": "Missing session"})
            return

        session = self._router.sessions.get_session(session_id)
        if not session:
            await self._write(writer, {"ok": False, "error": f"Session not found: {session_id}"})
            return

        log.info("Delete requested via control socket: %s", session_id)
        ok = await self._router.sessions.delete_session(session_id, reason="control")

        if ok:
            await self._write(writer, {"ok": True, "type": "deleted"})
        else:
            await self._write(writer, {"ok": False, "error": "Failed to delete session"})

    async def _handle_models(self, req: dict, writer: asyncio.StreamWriter) -> None:
        """Query available models live from the agent's Copilot SDK."""
        session_id = req.get("session", "")
        if not session_id:
            await self._write(writer, {"ok": False, "error": "Missing session"})
            return

        session = self._router.containers.get_session(session_id)
        if not session:
            await self._write(writer, {"ok": False, "error": f"Session not found: {session_id}"})
            return

        script = (
            "import asyncio, json\n"
            "from copilot import CopilotClient\n"
            "async def main():\n"
            "    c = CopilotClient()\n"
            "    await c.start()\n"
            "    try:\n"
            "        ms = await c.list_models()\n"
            "        print(json.dumps(sorted([m.id for m in ms])))\n"
            "    except Exception:\n"
            "        raw = await c.rpc('models.list', {})\n"
            "        print(json.dumps(sorted([m.get('id') for m in raw.get('models',[]) if m.get('id')])))\n"
            "    await c.stop()\n"
            "asyncio.run(main())\n"
        )

        try:
            runtime = self._router.sessions.config.runtime
            result = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: subprocess.run(
                    [runtime, "exec", session_id, "python3", "-c", script],
                    capture_output=True, text=True, timeout=20,
                ),
            )
            if result.returncode == 0 and result.stdout.strip():
                available = json.loads(result.stdout.strip())
                # Read current model from workspace file if it exists
                ws_base = Path(self._router.sessions.config.workspace_base) / session_id
                models_path = ws_base / ".enclave-models.json"
                current = None
                if models_path.exists():
                    try:
                        current = json.loads(models_path.read_text()).get("current")
                    except Exception:
                        pass

                # Update the file with fresh data
                models_data = {
                    "current": current,
                    "available": available,
                    "preferences": [],
                }
                try:
                    models_path.write_text(json.dumps(models_data, indent=2))
                except Exception:
                    pass

                await self._write(writer, {
                    "ok": True,
                    "type": "models",
                    "current": current,
                    "available": available,
                })
                return

            log.warning("Models query failed for %s: %s", session_id, result.stderr[:200])
            await self._write(writer, {"ok": False, "error": "Failed to query models from agent"})
        except Exception as e:
            log.warning("Models query error for %s: %s", session_id, e)
            await self._write(writer, {"ok": False, "error": str(e)})

    async def _handle_subscribe(
        self,
        req: dict,
        writer: asyncio.StreamWriter,
        reader: asyncio.StreamReader,
    ) -> None:
        """Subscribe to a session's event stream without sending a message."""
        session_id = req.get("session", "")
        if not session_id:
            await self._write(writer, {"ok": False, "error": "Missing session"})
            return

        # Subscribe to events
        q: asyncio.Queue[dict] = asyncio.Queue()
        subs = self._subscribers.setdefault(session_id, set())
        subs.add(q)

        try:
            await self._write(writer, {"ok": True, "type": "subscribed", "session": session_id})

            # Stream events until client disconnects or timeout
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=600.0)
                    await self._write(writer, msg)
                except asyncio.TimeoutError:
                    # Send keepalive ping
                    try:
                        await self._write(writer, {"ok": True, "type": "ping"})
                    except (ConnectionError, OSError):
                        break
                except (ConnectionError, OSError):
                    break
        finally:
            subs.discard(q)
            if not subs:
                self._subscribers.pop(session_id, None)

    async def _handle_send(
        self,
        req: dict,
        writer: asyncio.StreamWriter,
        reader: asyncio.StreamReader,
    ) -> None:
        session_id = req.get("session", "")
        content = req.get("content", "")
        sender = req.get("sender", "[Orchestrator]")
        attachments = req.get("attachments")  # optional list of attachment dicts

        if not session_id or (not content and not attachments):
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
            # Send the message content directly (no sender tag prefix)
            msg_content = content if content else "[Sent a file]"
            ok = await self._router.inject_message(session_id, msg_content, attachments=attachments)
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
