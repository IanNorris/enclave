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

from enclave.webui.event_store import PERSIST_TYPES, persist_event
from enclave.common import panel as panel_mod
from enclave.common import fusion as fusion_mod
from enclave.orchestrator.session_manager import is_concierge

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
        # session_id → True if the agent's last turn ended awaiting user input
        self._awaiting_input: dict[str, bool] = {}
        # Global subscribers receiving cross-session notification events
        # (awaiting_input / deferred_ask) for the Web UI notification panel.
        self._notification_subscribers: set[asyncio.Queue[dict]] = set()
        # Coarse per-session activity state (idle/thinking/tool/responding),
        # broadcast on the global channel so the Web UI sidebar can show a live
        # per-session indicator. Only re-broadcast on change to avoid spam.
        self._activity_state: dict[str, str] = {}

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

    def notify_user_message(self, session_id: str, content: str, sender: str = "") -> None:
        """Called by the router when a user message is delivered to the agent.

        Emitted from every dispatch path (Matrix project rooms and control-socket
        injection) so the webui's event persister durably records user turns
        regardless of where the message originated.
        """
        self._emit(session_id, {
            "ok": True, "type": "user_message", "content": content, "sender": sender,
        })

    def notify_response(self, session_id: str, content: str) -> None:
        """Called by the router when an agent sends a response."""
        self._set_activity(session_id, "responding")
        self._emit(session_id, {"ok": True, "type": "response", "content": content})

    def notify_delta(self, session_id: str, content: str) -> None:
        """Called by the router on streaming text delta."""
        self._set_activity(session_id, "responding")
        self._emit(session_id, {"ok": True, "type": "delta", "content": content})

    def notify_thinking(self, session_id: str, content: str, phase: str = "delta") -> None:
        """Called by the router on thinking/reasoning updates.

        phase: "start" (new thinking block), "delta" (streaming update), "end" (finalized)
        """
        self._set_activity(session_id, "thinking")
        self._emit(session_id, {"ok": True, "type": "thinking", "content": content, "phase": phase})

    def notify_tool_start(self, session_id: str, name: str, detail: str = "") -> None:
        """Called by the router when a tool starts executing."""
        self._set_activity(session_id, "tool")
        self._emit(session_id, {"ok": True, "type": "tool_start", "name": name, "detail": detail})

    def notify_tool_complete(self, session_id: str, name: str, success: bool = True) -> None:
        """Called by the router when a tool finishes."""
        self._set_activity(session_id, "thinking")
        self._emit(session_id, {"ok": True, "type": "tool_complete", "name": name, "success": success})

    def notify_activity(self, session_id: str, text: str) -> None:
        """Called by the router for activity/status updates."""
        self._emit(session_id, {"ok": True, "type": "activity", "text": text})

    def notify_file_send(
        self, session_id: str, filename: str, mimetype: str = "",
        mxc_url: str = "", event_id: str = "", file_path: str = "",
    ) -> None:
        """Called by the router when an agent uploads a file."""
        self._emit(session_id, {
            "ok": True, "type": "file_send",
            "filename": filename,
            "mimetype": mimetype,
            "mxc_url": mxc_url,
            "event_id": event_id,
            "file_path": file_path,
        })

    def notify_ask_user(self, session_id: str, question: str, choices: list[str] | None = None) -> None:
        """Called by the router when the agent asks the user a question."""
        self._emit(session_id, {
            "ok": True, "type": "ask_user",
            "question": question,
            "choices": choices or [],
        })

    def notify_structured_response(self, session_id: str, payload: dict) -> None:
        """Called by the router for structured agent responses (rich cards)."""
        self._emit(session_id, {"ok": True, "type": "structured_response", **payload})

    def notify_deferred_ask(self, session_id: str, ask: dict) -> None:
        """Called by the router when an agent posts a non-blocking question."""
        self._emit(session_id, {"ok": True, "type": "deferred_ask", **ask})
        # Also broadcast to all sessions so the global badge updates
        for sid in list(self._subscribers.keys()):
            if sid != session_id:
                self._emit(sid, {"ok": True, "type": "deferred_ask_badge", "session_id": session_id})
        # Push to the global notification stream (panel + browser push)
        self._emit_notification({
            "type": "notification",
            "reason": "deferred_ask",
            "session_id": session_id,
            "question": ask.get("question", ""),
            "choices": ask.get("choices", []) or [],
        })

    def notify_turn_start(self, session_id: str) -> None:
        """Called by the router when an agent turn begins."""
        # A new turn means any previous "awaiting input" state is resolved.
        self._awaiting_input.pop(session_id, None)
        self._set_activity(session_id, "thinking")
        self._emit(session_id, {"ok": True, "type": "turn_start"})

    def is_awaiting_input(self, session_id: str) -> bool:
        """Whether the session's last turn ended with the agent awaiting input."""
        return self._awaiting_input.get(session_id, False)

    def clear_awaiting_input(self, session_id: str) -> None:
        """Clear the awaiting-input flag (e.g. after the user dismisses it)."""
        self._awaiting_input.pop(session_id, None)
        self._emit(session_id, {
            "ok": True, "type": "awaiting_input",
            "session_id": session_id, "awaiting_input": False,
        })
        self._emit_notification({
            "type": "notification",
            "reason": "cleared",
            "session_id": session_id,
            "awaiting_input": False,
        })

    def notify_credits(self, session_id: str, payload: dict) -> None:
        """Called by the router with the latest account "AI Credits" snapshot.

        Live-only (not in PERSIST_TYPES): the durable copy lives in the cost
        tracker DB; this just pushes an update to any subscribed web UI so the
        header refreshes without a reload.
        """
        self._emit(session_id, {"ok": True, "type": "credits", **payload})

    def notify_fusion(self, session_id: str, payload: dict) -> None:
        """Called by the router on Auto Fusion grade + fusion events.

        - kind="grade": live complexity update (1-5) + recommended tier. Pushed
          to the chat stream so the UI shows current complexity; also broadcast
          on the global channel so the sidebar can show it. Not persisted.
        - kind="fusion": a completed fusion run with the model combo + trace
          (participant outcomes + judge analysis). Persisted (in PERSIST_TYPES)
          so the tappable trace survives reloads.
        """
        kind = payload.get("kind", "")
        if kind == "grade":
            self._emit(session_id, {"ok": True, "type": "complexity", **payload})
            self._emit_notification({
                "type": "complexity", "session_id": session_id,
                "score": payload.get("score"), "tier": payload.get("tier"),
                "reason": payload.get("reason", ""),
            })
        elif kind == "fusion":
            self._emit(session_id, {"ok": True, "type": "fusion", **payload})

    def _emit_notification(self, event: dict) -> None:
        """Push a cross-session notification event to all global subscribers."""
        for q in list(self._notification_subscribers):
            q.put_nowait(event)

    def notify_major_reply(self, session_id: str, text: str) -> None:
        """Broadcast a "major reply" (agent response / structured update) on the
        global notification channel.

        These mirror the "major events" forwarded to Matrix, so the Android
        client can post a per-session notification (keeping only the latest per
        session) without subscribing to every session's event stream. The Web UI
        ignores this event type.
        """
        name = session_id
        try:
            sess = self._router.sessions.get_session(session_id)
            if sess and getattr(sess, "name", None):
                name = sess.name
        except Exception:
            pass
        snippet = (text or "").strip()
        if len(snippet) > 280:
            snippet = snippet[:279] + "\u2026"
        self._emit_notification({
            "type": "major_reply",
            "session_id": session_id,
            "session_name": name,
            "text": snippet,
            "ts": __import__("time").time(),
        })

    def _set_activity(self, session_id: str, state: str) -> None:
        """Broadcast a coarse per-session activity state on change.

        States: 'thinking' | 'tool' | 'responding' | 'idle'. Sent on the global
        notification channel so the Web UI sidebar can show a per-session
        spinning-cog / pulsing-brain indicator without subscribing to every
        session's event stream.
        """
        if self._activity_state.get(session_id) == state:
            return
        self._activity_state[session_id] = state
        self._emit_notification({
            "type": "session_activity",
            "session_id": session_id,
            "state": state,
        })

    def _workspace_base_for(self, session_id: str) -> Path | None:
        """Resolve the workspace_base for a session's event store, or None.

        events.db lives at ``{workspace_base}/{session_id}/.enclave/events.db``
        and ``session.workspace_path`` is ``{workspace_base}/{session_id}``,
        so the base is its parent.
        """
        try:
            sess = self._router.sessions.get_session(session_id)
        except Exception:
            return None
        if not sess or not sess.workspace_path:
            return None
        return Path(sess.workspace_path).parent

    def _emit(self, session_id: str, event: dict) -> None:
        """Push an event to all subscribers of a session.

        Durable persistence happens HERE, at the source, before fan-out: this
        is the single chokepoint every agent event funnels through, so capture
        is exactly-once and independent of whether any subscriber (browser or
        the webui persister) happens to be connected. Persisting downstream in
        a subscriber was lossy — any event emitted while no subscriber was
        attached (new session, orchestrator restart, reconnect window) was
        silently dropped from events.db even though it streamed live.
        """
        if event.get("type") in PERSIST_TYPES or (
            event.get("type") == "thinking" and event.get("phase") == "end"
        ):
            base = self._workspace_base_for(session_id)
            if base is not None:
                persist_event(base, session_id, event)
        for q in self._subscribers.get(session_id, set()):
            q.put_nowait(event)

    def notify_turn_end(self, session_id: str, awaiting_input: bool = False) -> None:
        """Called by the router when an agent's turn ends.

        Debounced: waits 2s before notifying subscribers, cancelled if a new
        turn starts (agents do multiple turns per interaction). When
        ``awaiting_input`` is true the agent ended its turn asking the user a
        question; we record it and broadcast an ``awaiting_input`` badge to all
        subscribers so the notification panel can update live.
        """
        # Cancel any pending debounce
        timer = self._turn_end_timers.pop(session_id, None)
        if timer:
            timer.cancel()

        self._awaiting_input[session_id] = awaiting_input
        if awaiting_input:
            for sid in list(self._subscribers.keys()):
                if sid != session_id:
                    self._emit(sid, {
                        "ok": True, "type": "awaiting_input",
                        "session_id": session_id, "awaiting_input": True,
                    })
            self._emit_notification({
                "type": "notification",
                "reason": "awaiting",
                "session_id": session_id,
                "awaiting_input": True,
            })

        # Debounce the idle transition (and the per-session turn_end event) so
        # rapid multi-turn sequences don't flicker the activity indicator. This
        # runs regardless of whether a per-session subscriber is attached, so the
        # global sidebar indicator still returns to idle.
        def _fire() -> None:
            self._turn_end_timers.pop(session_id, None)
            self._set_activity(session_id, "idle")
            if self._subscribers.get(session_id):
                self._emit(session_id, {
                    "ok": True, "type": "turn_end",
                    "awaiting_input": awaiting_input,
                })

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
            elif action == "subscribe_notifications":
                await self._handle_subscribe_notifications(writer)
            elif action == "stop":
                await self._handle_stop(req, writer)
            elif action == "start":
                await self._handle_start(req, writer)
            elif action == "delete":
                await self._handle_delete(req, writer)
            elif action == "models":
                await self._handle_models(req, writer)
            elif action == "credits":
                await self._handle_credits(req, writer)
            elif action == "complexity":
                await self._handle_complexity(req, writer)
            elif action == "profiles":
                await self._handle_profiles(writer)
            elif action == "panel_get":
                await self._handle_panel_get(writer)
            elif action == "panel_set":
                await self._handle_panel_set(req, writer)
            elif action == "fusion_get":
                await self._handle_fusion_get(writer)
            elif action == "fusion_set":
                await self._handle_fusion_set(req, writer)
            elif action == "create":
                await self._handle_create(req, writer)
            elif action == "schedule_list":
                await self._handle_schedule_list(writer)
            elif action == "schedule_add":
                await self._handle_schedule_add(req, writer)
            elif action == "schedule_cancel":
                await self._handle_schedule_cancel(req, writer)
            elif action == "clear_awaiting":
                await self._handle_clear_awaiting(req, writer)
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
                "concierge": is_concierge(session.id),
                "awaiting_input": self._awaiting_input.get(session.id, False),
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

        # The typed list_models() helper raises on SDK 0.3.0 (pydantic rejects
        # the models.list payload: "Missing required field 'multiplier' in
        # ModelBilling"), so fall back to the raw JSON-RPC request, which returns
        # the full model set untouched. Note: c.rpc(...) is not callable on 0.3.0;
        # use the low-level c._client.request(method, params) transport.
        script = (
            "import asyncio, json\n"
            "from copilot import CopilotClient\n"
            "async def main():\n"
            "    c = CopilotClient()\n"
            "    await c.start()\n"
            "    try:\n"
            "        try:\n"
            "            ms = await c.list_models()\n"
            "            ids = [m.id for m in ms]\n"
            "        except Exception:\n"
            "            raw = await c._client.request('models.list', {})\n"
            "            ids = [m.get('id') for m in raw.get('models', []) if m.get('id')]\n"
            "        print(json.dumps(sorted(ids)))\n"
            "    finally:\n"
            "        await c.stop()\n"
            "asyncio.run(main())\n"
        )

        try:
            runtime = self._router.sessions.config.runtime
            from enclave.orchestrator.container import _container_name
            result = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: subprocess.run(
                    [runtime, "exec", _container_name(session_id), "python3", "-c", script],
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

    async def _handle_credits(self, req: dict, writer: asyncio.StreamWriter) -> None:
        """Return AI Credits info: account entitlement snapshot + per-session usage.

        The account entitlement snapshot (often "Unlimited") is global. The
        consumed AI Units ("AI Credits") figure is per session — pass ``session``
        to include the running total for that session. Served from the cost
        tracker so it survives orchestrator restarts and is available on initial
        web UI load.
        """
        session_id = req.get("session", "")
        try:
            credits = self._router._cost.get_credits() or {}
            session_credits = (
                self._router._cost.get_session_credits(session_id)
                if session_id else None
            )
        except Exception as e:
            await self._write(writer, {"ok": False, "error": str(e)})
            return
        payload = {"ok": True, "type": "credits", "snapshots": {}}
        payload.update(credits)
        payload["session"] = session_credits or {}
        await self._write(writer, payload)

    async def _handle_complexity(self, req: dict, writer: asyncio.StreamWriter) -> None:
        """Return recorded Auto Fusion complexity grades for the graph.

        Pass ``session`` for one session's scores, or omit for global.
        """
        session_id = req.get("session", "")
        try:
            scores = self._router._cost.complexity_scores(
                session_id or None, limit=int(req.get("limit", 500)),
            )
        except Exception as e:
            await self._write(writer, {"ok": False, "error": str(e)})
            return
        await self._write(writer, {"ok": True, "type": "complexity", "scores": scores})

    async def _handle_profiles(self, writer: asyncio.StreamWriter) -> None:
        """Return the configured container profiles for project creation."""
        try:
            cfg = self._router.containers.config
            profiles = []
            for name, prof in cfg.profiles.items():
                profiles.append({
                    "name": name,
                    "description": getattr(prof, "description", "") or "",
                    "default": name == cfg.default_profile,
                })
        except Exception as e:
            await self._write(writer, {"ok": False, "error": str(e)})
            return
        await self._write(writer, {"ok": True, "type": "profiles", "profiles": profiles})

    async def _handle_panel_get(self, writer: asyncio.StreamWriter) -> None:
        """Return the editable consult_panel definition from the host."""
        try:
            data_dir = self._router._data_dir
            panel = panel_mod.load_panel(data_dir)
        except Exception as e:
            await self._write(writer, {"ok": False, "error": str(e)})
            return
        await self._write(writer, {"ok": True, "type": "panel", "panel": panel})

    async def _handle_panel_set(
        self, req: dict, writer: asyncio.StreamWriter,
    ) -> None:
        """Persist a new panel definition and push it to active workspaces."""
        try:
            data_dir = self._router._data_dir
            incoming = req.get("panel", req.get("members"))
            panel = panel_mod.save_panel(data_dir, incoming)
        except Exception as e:
            await self._write(writer, {"ok": False, "error": str(e)})
            return

        # Propagate to every known session workspace so running agents pick up
        # the change on their next consult_panel call (no restart required).
        pushed = 0
        try:
            for session in self._router.containers.list_sessions():
                ws = getattr(session, "workspace_path", "")
                if ws and Path(ws).is_dir():
                    try:
                        panel_mod.write_workspace_panel(ws, panel)
                        pushed += 1
                    except Exception as e:
                        log.warning("Failed to push panel to %s: %s", ws, e)
        except Exception as e:
            log.warning("Panel propagation failed: %s", e)

        await self._write(
            writer, {"ok": True, "type": "panel", "panel": panel, "pushed": pushed},
        )

    async def _handle_fusion_get(self, writer: asyncio.StreamWriter) -> None:
        """Return the editable Fusion config (presets + auto routing) from host."""
        try:
            data_dir = self._router._data_dir
            doc = fusion_mod.load_fusion(data_dir)
        except Exception as e:
            await self._write(writer, {"ok": False, "error": str(e)})
            return
        await self._write(writer, {"ok": True, "type": "fusion", "fusion": doc})

    async def _handle_fusion_set(
        self, req: dict, writer: asyncio.StreamWriter,
    ) -> None:
        """Persist a new Fusion config and push it to active workspaces."""
        try:
            data_dir = self._router._data_dir
            doc = fusion_mod.save_fusion(data_dir, req.get("fusion"))
        except Exception as e:
            await self._write(writer, {"ok": False, "error": str(e)})
            return

        # Propagate to every known session workspace so running agents pick up
        # the change on their next fusion call (no restart required).
        pushed = 0
        try:
            for session in self._router.containers.list_sessions():
                ws = getattr(session, "workspace_path", "")
                if ws and Path(ws).is_dir():
                    try:
                        fusion_mod.write_workspace_fusion(ws, doc)
                        pushed += 1
                    except Exception as e:
                        log.warning("Failed to push fusion to %s: %s", ws, e)
        except Exception as e:
            log.warning("Fusion propagation failed: %s", e)

        await self._write(
            writer, {"ok": True, "type": "fusion", "fusion": doc, "pushed": pushed},
        )

    async def _handle_create(self, req: dict, writer: asyncio.StreamWriter) -> None:
        """Create a new project session (web UI "new session" button)."""
        name = (req.get("name") or "").strip()
        profile = (req.get("profile") or "").strip()
        if not name:
            await self._write(writer, {"ok": False, "error": "Missing project name"})
            return

        log.info("Create requested via control socket: name=%s profile=%s",
                 name, profile or "<default>")
        try:
            session_id, error = await self._router.create_project(name, profile)
        except Exception as e:
            log.warning("Project creation via control socket failed: %s", e)
            await self._write(writer, {"ok": False, "error": str(e)})
            return

        if error:
            await self._write(writer, {"ok": False, "error": error})
            return
        await self._write(writer, {"ok": True, "type": "created", "session": session_id})

    async def _handle_schedule_list(self, writer: asyncio.StreamWriter) -> None:
        """List all recurring schedules and one-shot timers."""
        sched = self._router._scheduler
        sessions = {s.id: s.name for s in self._router.containers.list_sessions()}

        def _name(sid: str) -> str:
            if is_concierge(sid):
                return "Concierge"
            return sessions.get(sid, sid)

        schedules = [
            {
                "id": e.id,
                "session_id": e.session_id,
                "session_name": _name(e.session_id),
                "interval_seconds": e.interval_seconds,
                "reason": e.reason,
                "next_fire": e.next_fire,
                "target": getattr(e, "target", "session"),
                "spawn_brief": getattr(e, "spawn_brief", ""),
                "kind": "recurring",
            }
            for e in sched.list_schedules()
        ]
        timers = [
            {
                "id": e.id,
                "session_id": e.session_id,
                "session_name": _name(e.session_id),
                "fire_at": e.fire_at,
                "reason": e.reason,
                "target": getattr(e, "target", "session"),
                "spawn_brief": getattr(e, "spawn_brief", ""),
                "kind": "timer",
            }
            for e in sched.list_timers()
        ]
        await self._write(
            writer,
            {"ok": True, "type": "schedules", "schedules": schedules, "timers": timers},
        )

    async def _handle_schedule_add(self, req: dict, writer: asyncio.StreamWriter) -> None:
        """Add a recurring schedule via the web UI."""
        target = req.get("target", "session")
        reason = (req.get("reason") or "").strip()
        interval = int(req.get("interval_seconds", 0) or 0)
        spawn_brief = req.get("spawn_brief", "")
        session_id = req.get("session_id", "")

        if not reason:
            await self._write(writer, {"ok": False, "error": "reason is required"})
            return
        if target == "concierge":
            session_id = "__concierge__"
        elif target == "session":
            if not session_id:
                await self._write(writer, {"ok": False, "error": "session_id is required"})
                return
            if not is_concierge(session_id) and \
                    not self._router.containers.get_session(session_id):
                await self._write(writer, {"ok": False, "error": f"No such session: {session_id}"})
                return
        else:  # spawn
            session_id = session_id or "__concierge__"

        import time as _time
        sched_id = req.get("id") or f"sched-{session_id}-{int(_time.time())}"
        result = self._router._scheduler.add_schedule(
            schedule_id=sched_id,
            session_id=session_id,
            interval_seconds=interval,
            reason=reason,
            target=target,
            spawn_brief=spawn_brief,
        )
        if isinstance(result, str):
            await self._write(writer, {"ok": False, "error": result})
            return
        await self._write(
            writer,
            {"ok": True, "type": "scheduled", "id": result.id, "next_fire": result.next_fire},
        )

    async def _handle_schedule_cancel(self, req: dict, writer: asyncio.StreamWriter) -> None:
        """Cancel a recurring schedule or timer."""
        sched_id = req.get("id", "")
        if not sched_id:
            await self._write(writer, {"ok": False, "error": "id is required"})
            return
        found = self._router._scheduler.cancel_schedule(sched_id) or \
            self._router._scheduler.cancel_timer(sched_id)
        await self._write(writer, {"ok": found, "type": "cancelled", "id": sched_id})

    async def _handle_clear_awaiting(self, req: dict, writer: asyncio.StreamWriter) -> None:
        """Clear a session's awaiting-input flag (notification dismiss)."""
        session_id = req.get("session", "")
        if not session_id:
            await self._write(writer, {"ok": False, "error": "Missing session"})
            return
        self.clear_awaiting_input(session_id)
        await self._write(writer, {"ok": True, "type": "awaiting_cleared", "session": session_id})

    async def _handle_subscribe_notifications(self, writer: asyncio.StreamWriter) -> None:
        """Subscribe to the global cross-session notification stream."""
        q: asyncio.Queue[dict] = asyncio.Queue()
        self._notification_subscribers.add(q)
        try:
            await self._write(writer, {"ok": True, "type": "subscribed_notifications"})
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=600.0)
                    await self._write(writer, msg)
                except asyncio.TimeoutError:
                    try:
                        await self._write(writer, {"ok": True, "type": "ping"})
                    except (ConnectionError, OSError):
                        break
                except (ConnectionError, OSError):
                    break
        finally:
            self._notification_subscribers.discard(q)

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
