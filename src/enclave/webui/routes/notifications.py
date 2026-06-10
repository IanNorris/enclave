"""Notification aggregation routes.

Aggregates the "needs a reply" state across all sessions for the Web UI
notification panel. A session needs a reply when either:

- its latest agent turn ended awaiting user input (``awaiting_input`` from the
  orchestrator session list), or
- it has pending (un-answered, un-dismissed) deferred asks.

Dismissing a pure chat-awaiting item clears the orchestrator's awaiting flag;
deferred asks are dismissed through the existing ``/asks/{id}/dismiss`` route.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect

log = logging.getLogger(__name__)
router = APIRouter(prefix="/notifications", tags=["notifications"])
ws_router = APIRouter()  # Separate router for WebSocket (no OAuth2 dependency)


def _control_sock_path(request: Request) -> Path:
    config = request.app.state.config
    return Path(config.data_dir) / "control.sock"


def _get_store(request: Request):
    from enclave.webui.deferred_asks import get_deferred_asks_store

    config = request.app.state.config
    workspace_base = Path(config.container.workspace_base)
    return get_deferred_asks_store(workspace_base)


async def _control_request(sock_path: Path, payload: dict, timeout: float = 5.0) -> dict | None:
    if not sock_path.exists():
        return None
    try:
        reader, writer = await asyncio.open_unix_connection(str(sock_path))
        writer.write(json.dumps(payload).encode() + b"\n")
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=timeout)
        writer.close()
        await writer.wait_closed()
        if line:
            return json.loads(line.decode())
    except Exception as e:
        log.warning("Control socket error: %s", e)
    return None


@router.get("")
async def list_notifications(request: Request):
    """Return the per-session "needs a reply" notifications."""
    sock_path = _control_sock_path(request)
    listing = await _control_request(sock_path, {"action": "list"})
    sessions = listing.get("sessions", []) if listing and listing.get("ok") else []
    names = {s["id"]: s.get("name", s["id"]) for s in sessions}

    store = _get_store(request)
    pending = store.list_pending()

    # Bucket pending asks by session.
    asks_by_session: dict[str, list[dict]] = {}
    for ask in pending:
        asks_by_session.setdefault(ask["session_id"], []).append(ask)

    notifications: list[dict] = []

    for s in sessions:
        sid = s["id"]
        awaiting = bool(s.get("awaiting_input"))
        session_asks = asks_by_session.get(sid, [])
        if not awaiting and not session_asks:
            continue

        reasons = []
        if awaiting:
            reasons.append("awaiting")
        if session_asks:
            reasons.append("deferred_ask")

        latest_question = ""
        latest_choices: list[str] = []
        if session_asks:
            latest = session_asks[-1]
            latest_question = latest.get("question", "")
            latest_choices = latest.get("choices", []) or []

        notifications.append({
            "session_id": sid,
            "session_name": names.get(sid, sid),
            "reasons": reasons,
            "awaiting_input": awaiting,
            "ask_count": len(session_asks),
            "question": latest_question,
            "choices": latest_choices,
            "asks": session_asks,
        })

    # Include deferred asks for sessions not present in the live listing.
    for sid, session_asks in asks_by_session.items():
        if sid in names:
            continue
        latest = session_asks[-1]
        notifications.append({
            "session_id": sid,
            "session_name": sid,
            "reasons": ["deferred_ask"],
            "awaiting_input": False,
            "ask_count": len(session_asks),
            "question": latest.get("question", ""),
            "choices": latest.get("choices", []) or [],
            "asks": session_asks,
        })

    return {"notifications": notifications, "count": len(notifications)}


@router.get("/count")
async def notification_count(request: Request):
    data = await list_notifications(request)
    return {"count": data["count"]}


@router.post("/{session_id}/dismiss")
async def dismiss_notification(request: Request, session_id: str):
    """Dismiss a session's chat-awaiting notification.

    Clears the orchestrator's awaiting-input flag and dismisses any pending
    deferred asks for the session so the panel entry disappears.
    """
    sock_path = _control_sock_path(request)
    result = await _control_request(
        sock_path, {"action": "clear_awaiting", "session": session_id},
    )

    store = _get_store(request)
    dismissed = 0
    for ask in store.list_pending(session_id=session_id):
        if store.dismiss(ask["id"]):
            dismissed += 1

    if result is None and dismissed == 0:
        raise HTTPException(status_code=503, detail="Orchestrator not running")

    return {"ok": True, "session_id": session_id, "asks_dismissed": dismissed}


@ws_router.websocket("/notifications/stream")
async def notifications_stream(websocket: WebSocket, token: str = ""):
    """WebSocket: relay global cross-session notification events to the browser.

    Subscribes to the orchestrator's global notification channel so the panel
    and browser push update live (no polling lag). Auth via ?token= query
    param (OAuth2 bearer deps don't work with WebSockets).
    """
    from enclave.webui.auth import validate_token
    try:
        validate_token(token)
    except Exception:
        await websocket.close(code=4001, reason="Unauthorized")
        return

    await websocket.accept()

    config = websocket.app.state.config
    sock_path = Path(config.data_dir) / "control.sock"

    async def _relay():
        while True:
            if not sock_path.exists():
                await asyncio.sleep(2.0)
                continue
            try:
                reader, writer = await asyncio.open_unix_connection(str(sock_path))
                req = json.dumps({"action": "subscribe_notifications"})
                writer.write(req.encode() + b"\n")
                await writer.drain()
                while True:
                    line = await reader.readline()
                    if not line:
                        break
                    try:
                        event = json.loads(line.decode())
                    except json.JSONDecodeError:
                        continue
                    if event.get("type") in ("ping", "subscribed_notifications"):
                        continue
                    try:
                        await websocket.send_json(event)
                    except Exception:
                        return
            except (OSError, ConnectionError):
                pass
            finally:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass
            await asyncio.sleep(1.0)

    try:
        relay_task = asyncio.create_task(_relay())
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
    except Exception:
        pass
    finally:
        relay_task.cancel()
        try:
            await websocket.close()
        except Exception:
            pass
