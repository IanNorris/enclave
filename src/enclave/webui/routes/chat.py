"""Conversation API routes.

Provides chat history from the session-store.db (turns table) and
sends messages via the orchestrator control socket (which wakes idle
agents) with Matrix echo, falling back to direct Matrix send.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

import aiohttp
from fastapi import APIRouter, HTTPException, Request, UploadFile, File, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

router = APIRouter()
ws_router = APIRouter()  # Separate router for WebSocket (no OAuth2 dependency)

# In-memory cache of recent agent responses not yet in session-store.db.
# Keyed by session_id → list of {role, content, timestamp}.
# Populated by the WebSocket control socket listener, consumed by history endpoint.
_response_cache: dict[str, list[dict]] = {}


# ─── Helpers ────────────────────────────────────────────────────────────────


def _workspace_base(request: Request) -> Path:
    return Path(request.app.state.config.container.workspace_base)


def _session_store_db(request: Request, session_id: str) -> Path:
    """Path to a session's Copilot state DB."""
    ws = _workspace_base(request) / session_id / ".copilot-state" / "session-store.db"
    return ws


def _sessions_json(request: Request) -> Path:
    return Path(request.app.state.config.container.session_base) / "sessions.json"


def _get_room_id(request: Request, session_id: str) -> str | None:
    """Get the Matrix room_id for a session."""
    sessions_file = _sessions_json(request)
    if not sessions_file.exists():
        return None
    try:
        data = json.loads(sessions_file.read_text())
        for s in data:
            if s.get("id") == session_id:
                return s.get("room_id")
    except (json.JSONDecodeError, OSError):
        pass
    return None


def _matrix_config(request: Request):
    return request.app.state.config.matrix


def _read_turns(db_path: Path, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    """Read conversation turns from session-store.db."""
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            """SELECT turn_index, user_message, assistant_response, timestamp
               FROM turns
               ORDER BY turn_index DESC
               LIMIT ? OFFSET ?""",
            (limit, offset),
        )
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        # Return in chronological order
        rows.reverse()
        return rows
    except (sqlite3.Error, OSError):
        return []


# ─── Matrix API helpers ─────────────────────────────────────────────────────


_access_token_cache: dict[str, str] = {}


async def _get_matrix_token(config) -> str:
    """Login to Matrix and cache the access token."""
    key = f"{config.homeserver}:{config.user_id}"
    if key in _access_token_cache:
        return _access_token_cache[key]

    async with aiohttp.ClientSession() as session:
        url = f"{config.homeserver}/_matrix/client/v3/login"
        payload = {
            "type": "m.login.password",
            "user": config.user_id,
            "password": config.password,
            "device_id": "enclave-webui",
        }
        async with session.post(url, json=payload) as resp:
            if resp.status != 200:
                raise HTTPException(status_code=502, detail="Failed to authenticate with Matrix")
            data = await resp.json()
            token = data["access_token"]
            _access_token_cache[key] = token
            return token


async def _send_matrix_message(config, room_id: str, body: str) -> str:
    """Send a message to a Matrix room."""
    token = await _get_matrix_token(config)
    import time
    txn_id = f"webui-{int(time.time() * 1000)}"

    async with aiohttp.ClientSession() as session:
        url = f"{config.homeserver}/_matrix/client/v3/rooms/{room_id}/send/m.room.message/{txn_id}"
        headers = {"Authorization": f"Bearer {token}"}
        payload = {
            "msgtype": "m.text",
            "body": body,
        }
        async with session.put(url, json=payload, headers=headers) as resp:
            if resp.status != 200:
                err = await resp.text()
                raise HTTPException(status_code=502, detail=f"Matrix send failed: {err}")
            data = await resp.json()
            return data.get("event_id", "")


async def _upload_matrix_file(config, filename: str, content: bytes, content_type: str) -> str:
    """Upload a file to Matrix and return the mxc:// URI."""
    token = await _get_matrix_token(config)

    async with aiohttp.ClientSession() as session:
        url = f"{config.homeserver}/_matrix/media/v3/upload?filename={filename}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": content_type,
        }
        async with session.post(url, data=content, headers=headers) as resp:
            if resp.status != 200:
                err = await resp.text()
                raise HTTPException(status_code=502, detail=f"Matrix upload failed: {err}")
            data = await resp.json()
            return data.get("content_uri", "")


async def _send_matrix_file(config, room_id: str, mxc_uri: str, filename: str, size: int, content_type: str) -> str:
    """Send a file message to a Matrix room."""
    token = await _get_matrix_token(config)
    import time
    txn_id = f"webui-file-{int(time.time() * 1000)}"

    msgtype = "m.image" if content_type.startswith("image/") else "m.file"

    async with aiohttp.ClientSession() as session:
        url = f"{config.homeserver}/_matrix/client/v3/rooms/{room_id}/send/m.room.message/{txn_id}"
        headers = {"Authorization": f"Bearer {token}"}
        payload = {
            "msgtype": msgtype,
            "body": filename,
            "url": mxc_uri,
            "info": {
                "mimetype": content_type,
                "size": size,
            },
        }
        async with session.put(url, json=payload, headers=headers) as resp:
            if resp.status != 200:
                err = await resp.text()
                raise HTTPException(status_code=502, detail=f"Matrix file send failed: {err}")
            data = await resp.json()
            return data.get("event_id", "")


# ─── Control socket helper ───────────────────────────────────────────────────


async def _send_via_control_socket(data_dir: Path, session_id: str, content: str) -> bool:
    """Send a message via the orchestrator control socket (wakes idle agents).

    Returns True if the message was accepted, False on failure.
    """
    sock_path = data_dir / "control.sock"
    if not sock_path.exists():
        return False

    try:
        reader, writer = await asyncio.open_unix_connection(str(sock_path))
        req = json.dumps({
            "action": "send",
            "session": session_id,
            "content": content,
            "sender": "WebUI",
        })
        writer.write(req.encode() + b"\n")
        await writer.drain()

        # Read the ack response
        line = await asyncio.wait_for(reader.readline(), timeout=5.0)
        if line:
            resp = json.loads(line.decode())
            if resp.get("ok"):
                writer.close()
                await writer.wait_closed()
                return True

        writer.close()
        await writer.wait_closed()
    except (OSError, asyncio.TimeoutError, json.JSONDecodeError):
        pass
    return False


# ─── Models ─────────────────────────────────────────────────────────────────


class SendMessage(BaseModel):
    content: str


# ─── Endpoints ──────────────────────────────────────────────────────────────


@router.get("/{session_id}/history")
async def get_history(request: Request, session_id: str, limit: int = 100, offset: int = 0):
    """Get conversation history for a session.

    Primary source: session-store.db (SDK turns).
    Supplement: in-memory response cache for recent agent activity not yet in DB.
    """
    db_path = _session_store_db(request, session_id)
    turns = _read_turns(db_path, limit=limit, offset=offset)

    # Append cached responses that are newer than the last DB turn
    if offset == 0 and session_id in _response_cache:
        last_db_ts = ""
        if turns:
            last_db_ts = turns[-1].get("timestamp", "")

        cached = _response_cache.get(session_id, [])
        for entry in cached:
            if entry["timestamp"] <= last_db_ts:
                continue
            turns.append({
                "turn_index": None,
                "user_message": None,
                "assistant_response": entry["content"],
                "timestamp": entry["timestamp"],
                "source": "live",
            })

        # Prune cache entries that are now in the DB
        if last_db_ts and cached:
            _response_cache[session_id] = [
                e for e in cached if e["timestamp"] > last_db_ts
            ]

    return {"turns": turns, "session_id": session_id}


@router.post("/{session_id}/send")
async def send_message(request: Request, session_id: str, body: SendMessage):
    """Send a message to a session's agent via control socket (preferred) or Matrix."""
    # Try control socket first — this properly wakes idle/stopped agents
    data_dir = Path(request.app.state.config.data_dir)
    sent = await _send_via_control_socket(data_dir, session_id, body.content)
    if sent:
        return {"sent": True, "via": "control_socket"}

    # Fallback: direct Matrix send (won't wake agent if it's idle/stopped)
    room_id = _get_room_id(request, session_id)
    if not room_id:
        raise HTTPException(status_code=404, detail="Session room not found")

    config = _matrix_config(request)
    event_id = await _send_matrix_message(config, room_id, body.content)
    return {"sent": True, "event_id": event_id, "via": "matrix"}


@router.get("/{session_id}/models")
async def get_models(request: Request, session_id: str):
    """Get available models for a session (from agent's .enclave-models.json)."""
    ws_base = Path(request.app.state.config.container.workspace_base) / session_id
    models_path = ws_base / ".enclave-models.json"
    if not models_path.exists():
        return {"current": None, "available": [], "preferences": []}
    try:
        data = json.loads(models_path.read_text())
        return data
    except Exception:
        return {"current": None, "available": [], "preferences": []}


@router.post("/{session_id}/model")
async def set_model(request: Request, session_id: str, body: SendMessage):
    """Request a model change by sending a /model command to the agent."""
    room_id = _get_room_id(request, session_id)
    if not room_id:
        raise HTTPException(status_code=404, detail="Session room not found")

    config = _matrix_config(request)
    event_id = await _send_matrix_message(config, room_id, f"/model {body.content}")
    return {"sent": True, "event_id": event_id, "model": body.content}


@router.post("/{session_id}/upload")
async def upload_file(request: Request, session_id: str, file: UploadFile = File(...), message: str = ""):
    """Upload a file and send it to the agent's Matrix room."""
    from fastapi import Form

    room_id = _get_room_id(request, session_id)
    if not room_id:
        raise HTTPException(status_code=404, detail="Session room not found")

    config = _matrix_config(request)
    content = await file.read()
    content_type = file.content_type or "application/octet-stream"
    filename = file.filename or "attachment"

    # Upload to Matrix
    mxc_uri = await _upload_matrix_file(config, filename, content, content_type)

    # Send as file message
    event_id = await _send_matrix_file(config, room_id, mxc_uri, filename, len(content), content_type)

    # If there's an accompanying text message, send that too
    if message.strip():
        await _send_matrix_message(config, room_id, message.strip())

    return {"sent": True, "event_id": event_id, "filename": filename}


@ws_router.websocket("/{session_id}/stream")
async def stream_conversation(websocket: WebSocket, session_id: str, token: str = ""):
    """WebSocket: stream live agent events and completed turns.

    Connects to the orchestrator's control socket to receive real-time
    events (deltas, thinking, tool calls), and falls back to SQLite
    polling for completed turns.

    Auth is via ?token= query param (OAuth2 bearer deps don't work with WS).
    """
    # Validate token manually — OAuth2PasswordBearer doesn't handle WebSocket
    from enclave.webui.auth import validate_token
    try:
        validate_token(token)
    except Exception:
        await websocket.close(code=4001, reason="Unauthorized")
        return

    await websocket.accept()

    config = websocket.app.state.config
    data_dir = Path(config.data_dir)
    db_path = Path(config.container.workspace_base) / session_id / ".copilot-state" / "session-store.db"
    sock_path = data_dir / "control.sock"

    # Track last known turn for SQLite fallback
    last_turn = -1
    if db_path.exists():
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            cur = conn.execute("SELECT MAX(turn_index) FROM turns")
            row = cur.fetchone()
            if row and row[0] is not None:
                last_turn = row[0]
            conn.close()
        except sqlite3.Error:
            pass

    async def _stream_from_control_socket():
        """Subscribe to the control socket and forward events to the WebSocket."""
        while True:
            if not sock_path.exists():
                await asyncio.sleep(2.0)
                continue

            try:
                reader, writer = await asyncio.open_unix_connection(str(sock_path))
                req = json.dumps({"action": "subscribe", "session": session_id})
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
                    if event.get("type") in ("ping", "subscribed"):
                        continue

                    # Cache response events for history supplement
                    if event.get("type") == "response" and event.get("content"):
                        from datetime import datetime, timezone
                        ts = datetime.now(timezone.utc).strftime(
                            "%Y-%m-%dT%H:%M:%S.%f"
                        )[:-3] + "Z"
                        cache = _response_cache.setdefault(session_id, [])
                        cache.append({
                            "role": "assistant",
                            "content": event["content"],
                            "timestamp": ts,
                        })
                        # Keep cache bounded
                        if len(cache) > 200:
                            _response_cache[session_id] = cache[-100:]

                    try:
                        await websocket.send_json(event)
                    except Exception:
                        # WebSocket dead — exit entirely
                        return

            except (OSError, ConnectionError):
                pass
            finally:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass

            # Reconnect after a brief delay
            await asyncio.sleep(1.0)

    async def _poll_turns():
        """Poll SQLite for completed turns (fallback for persistence)."""
        nonlocal last_turn
        while True:
            await asyncio.sleep(3.0)

            if not db_path.exists():
                continue

            try:
                conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                conn.row_factory = sqlite3.Row
                cur = conn.execute(
                    """SELECT turn_index, user_message, assistant_response, timestamp
                       FROM turns WHERE turn_index > ?
                       ORDER BY turn_index ASC""",
                    (last_turn,),
                )
                new_turns = [dict(r) for r in cur.fetchall()]
                conn.close()

                for turn in new_turns:
                    turn["type"] = "turn"
                    await websocket.send_json(turn)
                    last_turn = turn["turn_index"]

            except sqlite3.Error:
                continue

    try:
        # Run both streams concurrently
        ctrl_task = asyncio.create_task(_stream_from_control_socket())
        poll_task = asyncio.create_task(_poll_turns())

        # Wait for WebSocket to close (either side)
        try:
            while True:
                # Keep reading from client to detect disconnect
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
    except Exception:
        pass
    finally:
        ctrl_task.cancel()
        poll_task.cancel()
        try:
            await websocket.close()
        except Exception:
            pass
