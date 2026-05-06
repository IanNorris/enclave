"""Conversation API routes.

Provides chat history from the session-store.db (turns table) and
sends messages via the Matrix client-server API so they flow through
the existing orchestrator pipeline.
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


# ─── Models ─────────────────────────────────────────────────────────────────


class SendMessage(BaseModel):
    content: str


# ─── Endpoints ──────────────────────────────────────────────────────────────


@router.get("/{session_id}/history")
async def get_history(request: Request, session_id: str, limit: int = 100, offset: int = 0):
    """Get conversation history for a session."""
    db_path = _session_store_db(request, session_id)
    turns = _read_turns(db_path, limit=limit, offset=offset)
    return {"turns": turns, "session_id": session_id}


@router.post("/{session_id}/send")
async def send_message(request: Request, session_id: str, body: SendMessage):
    """Send a message to a session's agent via Matrix."""
    room_id = _get_room_id(request, session_id)
    if not room_id:
        raise HTTPException(status_code=404, detail="Session room not found")

    config = _matrix_config(request)
    event_id = await _send_matrix_message(config, room_id, body.content)
    return {"sent": True, "event_id": event_id}


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


@router.websocket("/{session_id}/stream")
async def stream_conversation(websocket: WebSocket, session_id: str):
    """WebSocket: stream new conversation turns as they arrive."""
    await websocket.accept()

    # Get the DB path from app config
    config = websocket.app.state.config
    db_path = Path(config.container.workspace_base) / session_id / ".copilot-state" / "session-store.db"

    # Track last known turn
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

    try:
        while True:
            await asyncio.sleep(1.5)  # Poll interval

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
                    await websocket.send_json(turn)
                    last_turn = turn["turn_index"]

            except sqlite3.Error:
                continue

    except WebSocketDisconnect:
        pass
    except Exception:
        await websocket.close()
