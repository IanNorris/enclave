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
public_router = APIRouter()  # Routes that accept ?token= query param auth (for <img> etc.)

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


# Event types that contribute to history reconstruction (ordered conversation).
_HISTORY_TYPES = ["user_message", "response", "ask_user", "structured_response"]


def _reconstruct_turns(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build conversation turns from the chronological event log.

    The event store (events.db) is the single source of truth. Events arrive
    ordered by insertion id. A ``user_message`` opens a new turn; subsequent
    agent outputs (``response``/``ask_user``) belong to that turn. Each turn's
    ``assistant_response`` is the turn's final major output (always shown in the
    UI); intermediate responses and tool calls are surfaced separately via the
    ``/events`` endpoint, and ``structured_response`` cards are mapped to turns
    by timestamp on the client. Agent output emitted before any user message
    forms a leading turn with ``user_message=None``.
    """
    turns: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None

    def flush() -> None:
        nonlocal cur
        if cur is not None:
            major = cur.pop("_major")
            cur["assistant_response"] = major[-1] if major else None
            turns.append(cur)
            cur = None

    def ensure(ts: str) -> None:
        nonlocal cur
        if cur is None:
            cur = {
                "turn_index": len(turns),
                "user_message": None,
                "assistant_response": None,
                "timestamp": ts,
                "_major": [],
            }

    for evt in events:
        etype = evt.get("type", "")
        ts = evt.get("timestamp", "")
        data = evt.get("data", {})
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except (json.JSONDecodeError, TypeError):
                data = {}

        if etype == "user_message":
            flush()
            cur = {
                "turn_index": len(turns),
                "user_message": data.get("content", ""),
                "assistant_response": None,
                "timestamp": ts,
                "_major": [],
            }
        elif etype == "response":
            ensure(ts)
            content = data.get("content", "")
            if content:
                cur["_major"].append(content)  # type: ignore[index]
        elif etype == "ask_user":
            ensure(ts)
            question = data.get("question", "")
            if question:
                cur["_major"].append(f"**Question:** {question}")  # type: ignore[index]
        elif etype == "structured_response":
            # Rendered as a card mapped by timestamp on the client; just make
            # sure a turn exists so the mapping has a target.
            ensure(ts)

    flush()
    return turns


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


async def _send_via_control_socket(
    data_dir: Path, session_id: str, content: str, attachments: list[dict] | None = None
) -> bool:
    """Send a message via the orchestrator control socket (wakes idle agents).

    Returns True if the message was accepted, False on failure.
    """
    sock_path = data_dir / "control.sock"
    if not sock_path.exists():
        return False

    try:
        reader, writer = await asyncio.open_unix_connection(str(sock_path))
        req_data: dict = {
            "action": "send",
            "session": session_id,
            "content": content,
            "sender": "WebUI",
        }
        if attachments:
            req_data["attachments"] = attachments
        req = json.dumps(req_data)
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

    Reconstructed entirely from the durable event store (events.db), which the
    EventPersister keeps populated for all sessions regardless of whether a
    browser is connected. Falls back to the SDK's session-store.db only for
    legacy sessions that predate the event store.
    """
    from enclave.webui.event_store import get_event_store

    workspace_base = _workspace_base(request)
    store = get_event_store(workspace_base, session_id)
    events = store.get_events(types=_HISTORY_TYPES, limit=100000)

    if events:
        all_turns = _reconstruct_turns(events)
        # Paginate over the chronological turn list: offset counts back from the
        # newest turn, limit caps the window size (matches the client's
        # load-earlier behaviour which passes offset=len(loaded_turns)).
        total = len(all_turns)
        end = max(total - offset, 0)
        start = max(end - limit, 0)
        return {"turns": all_turns[start:end], "session_id": session_id}

    # Legacy fallback: no event store yet — read SDK turns directly.
    db_path = _session_store_db(request, session_id)
    turns = _read_turns(db_path, limit=limit, offset=offset)
    return {"turns": turns, "session_id": session_id}


@router.get("/{session_id}/events")
async def get_events(
    request: Request,
    session_id: str,
    since_id: int | None = None,
    since_timestamp: str | None = None,
    types: str | None = None,
    level: str = "full",
    limit: int = 500,
):
    """Get persisted events for a session.

    Query params:
        since_id: return events after this id
        since_timestamp: return events after this ISO timestamp
        types: comma-separated event types to filter (e.g. "tool_start,response")
        level: "full" (all events) or "major" (responses, files, asks, turns only)
        limit: max events to return (default 500)
    """
    from enclave.webui.event_store import get_event_store
    workspace_base = _workspace_base(request)
    store = get_event_store(workspace_base, session_id)
    type_list = types.split(",") if types else None
    events = store.get_events(
        since_id=since_id,
        since_timestamp=since_timestamp,
        types=type_list,
        level=level,
        limit=limit,
    )
    return {"events": events, "session_id": session_id}


@router.get("/{session_id}/timeline")
async def get_timeline(request: Request, session_id: str, date: str | None = None):
    """Get a timeline view of session activity.

    Three levels of detail:
    1. No date param → day-level summary (list of active dates with stats)
    2. date=YYYY-MM-DD → hour-level breakdown for that date
    3. date=YYYY-MM-DDThh → minute-level events for that hour
    """
    from enclave.webui.event_store import get_event_store
    from collections import defaultdict

    workspace_base = _workspace_base(request)
    store = get_event_store(workspace_base, session_id)

    if date and "T" in date:
        # Level 3: events for a specific hour
        hour_prefix = date[:13]  # YYYY-MM-DDThh
        # Get events for this hour window
        start = f"{hour_prefix}:00:00"
        end_hour = int(hour_prefix[-2:]) + 1
        if end_hour >= 24:
            # Roll to next day
            from datetime import datetime as dt, timedelta
            d = dt.fromisoformat(f"{hour_prefix[:10]}T00:00:00")
            d += timedelta(days=1)
            end = f"{d.strftime('%Y-%m-%d')}T00:00:00"
        else:
            end = f"{hour_prefix[:11]}{end_hour:02d}:00:00"

        events = store.get_events(since_timestamp=start, limit=5000)
        # Filter to events before end
        events = [e for e in events if e["timestamp"] < end]

        return {"level": "events", "date": hour_prefix, "events": events, "session_id": session_id}

    conn = store._conn()

    if date:
        # Level 2: hourly breakdown for a date
        day = date[:10]
        rows = conn.execute(
            """
            SELECT substr(timestamp, 12, 2) AS hour,
                   type,
                   COUNT(*) AS cnt
            FROM events
            WHERE timestamp LIKE ? || '%'
            GROUP BY hour, type
            ORDER BY hour, type
            """,
            (day,),
        ).fetchall()

        hours: dict[str, dict] = {}
        for r in rows:
            h = r["hour"]
            if h not in hours:
                hours[h] = {"hour": h, "event_counts": {}, "total": 0, "highlights": []}
            hours[h]["event_counts"][r["type"]] = r["cnt"]
            hours[h]["total"] += r["cnt"]

        # Fetch highlight events (responses, user messages, task completions)
        highlight_rows = conn.execute(
            """
            SELECT substr(timestamp, 12, 2) AS hour,
                   type,
                   timestamp,
                   data
            FROM events
            WHERE timestamp LIKE ? || '%'
              AND type IN ('response', 'user_message', 'ask_user', 'structured_response', 'file_send')
            ORDER BY timestamp
            """,
            (day,),
        ).fetchall()

        for r in highlight_rows:
            h = r["hour"]
            if h in hours:
                try:
                    data = json.loads(r["data"]) if isinstance(r["data"], str) else r["data"]
                except (json.JSONDecodeError, TypeError):
                    data = {}
                content = data.get("content", data.get("question", data.get("summary", "")))
                if content:
                    hours[h]["highlights"].append({
                        "type": r["type"],
                        "timestamp": r["timestamp"],
                        "preview": content[:200],
                    })

        hour_list = sorted(hours.values(), key=lambda x: x["hour"])
        return {
            "level": "hours",
            "date": day,
            "hours": hour_list,
            "session_id": session_id,
        }

    # Level 1: day-level summary
    rows = conn.execute(
        """
        SELECT substr(timestamp, 1, 10) AS day,
               type,
               COUNT(*) AS cnt
        FROM events
        GROUP BY day, type
        ORDER BY day DESC, type
        """
    ).fetchall()

    days: dict[str, dict] = {}
    for r in rows:
        d = r["day"]
        if d not in days:
            days[d] = {"date": d, "event_counts": {}, "total": 0}
        days[d]["event_counts"][r["type"]] = r["cnt"]
        days[d]["total"] += r["cnt"]

    # Get first and last user message per day for context
    msg_rows = conn.execute(
        """
        SELECT substr(timestamp, 1, 10) AS day,
               MIN(timestamp) AS first_ts,
               MAX(timestamp) AS last_ts
        FROM events
        WHERE type IN ('user_message', 'response')
        GROUP BY day
        ORDER BY day DESC
        """
    ).fetchall()

    for r in msg_rows:
        d = r["day"]
        if d in days:
            days[d]["first_activity"] = r["first_ts"]
            days[d]["last_activity"] = r["last_ts"]

    day_list = sorted(days.values(), key=lambda x: x["date"], reverse=True)
    return {
        "level": "days",
        "days": day_list,
        "session_id": session_id,
    }


@router.post("/{session_id}/send")
async def send_message(request: Request, session_id: str, body: SendMessage):
    """Send a message to a session's agent via control socket (preferred) or Matrix."""
    # The user turn is persisted by the orchestrator (notify_user_message →
    # EventPersister) once the message reaches the agent, so all dispatch paths
    # are recorded uniformly. No direct persistence here.

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
async def get_models(request: Request, session_id: str, refresh: bool = False):
    """Get available models for a session.

    Reads from the cached .enclave-models.json file by default.
    Pass ?refresh=true to query live from the agent's Copilot SDK
    (updates the cache file as a side effect).
    """
    ws_base = Path(request.app.state.config.container.workspace_base) / session_id
    models_path = ws_base / ".enclave-models.json"

    if refresh:
        # Query live via control socket
        data_dir = Path(request.app.state.config.data_dir)
        sock_path = data_dir / "control.sock"
        if sock_path.exists():
            try:
                reader, writer = await asyncio.open_unix_connection(str(sock_path))
                req = json.dumps({"action": "models", "session": session_id})
                writer.write(req.encode() + b"\n")
                await writer.drain()
                line = await asyncio.wait_for(reader.readline(), timeout=25.0)
                writer.close()
                await writer.wait_closed()
                if line:
                    resp = json.loads(line.decode())
                    if resp.get("ok"):
                        return {
                            "current": resp.get("current"),
                            "available": resp.get("available", []),
                            "preferences": [],
                        }
            except (OSError, asyncio.TimeoutError, json.JSONDecodeError) as e:
                import logging
                logging.getLogger("enclave.webui").warning("Models refresh failed: %s", e)

    # Fall back to cached file
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

    # Update the current model in the models file
    ws_base = Path(request.app.state.config.container.workspace_base) / session_id
    models_path = ws_base / ".enclave-models.json"
    try:
        if models_path.exists():
            data = json.loads(models_path.read_text())
            data["current"] = body.content
            models_path.write_text(json.dumps(data, indent=2))
    except Exception:
        pass

    return {"sent": True, "event_id": event_id, "model": body.content}


@router.post("/{session_id}/upload")
async def upload_file(request: Request, session_id: str, file: UploadFile = File(...), message: str = ""):
    """Upload a file and send it to the agent via control socket (with attachment metadata).

    Also uploads to Matrix for the conversation record (best-effort).
    """
    config = request.app.state.config
    content = await file.read()
    content_type = file.content_type or "application/octet-stream"
    # Sanitize: strip any directory components from the client-supplied filename
    # (multipart filenames are NOT path-normalized, so '../' would otherwise
    # escape the attachments dir and allow arbitrary host file writes).
    filename = Path(file.filename or "attachment").name or "attachment"

    # Save to workspace attachments dir so agent can read it directly
    ws_base = Path(config.container.workspace_base) / session_id
    attach_dir = ws_base / ".attachments"
    attach_dir.mkdir(parents=True, exist_ok=True)
    local_path = (attach_dir / filename).resolve()
    # Defence in depth: confirm the resolved path stays within attach_dir.
    try:
        local_path.relative_to(attach_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid filename")
    local_path.write_bytes(content)

    # Send to agent via control socket with attachment metadata
    data_dir = Path(config.data_dir)
    attachment_meta = {
        "filename": filename,
        "content_type": content_type,
        "local_path": str(Path("/workspace/.attachments") / filename),  # path inside container
    }
    text_content = message.strip() if message.strip() else f"[Sent a file: {filename}]"
    sent_to_agent = await _send_via_control_socket(
        data_dir, session_id, text_content, attachments=[attachment_meta]
    )

    # Also upload to Matrix for the conversation record (best-effort)
    room_id = _get_room_id(request, session_id)
    if room_id:
        try:
            mat_config = _matrix_config(request)
            mxc_uri = await _upload_matrix_file(mat_config, filename, content, content_type)
            await _send_matrix_file(mat_config, room_id, mxc_uri, filename, len(content), content_type)
        except Exception:
            pass  # Matrix upload is best-effort for the record

    if sent_to_agent:
        return {"sent": True, "via": "control_socket", "filename": filename}

    return {"sent": True, "via": "workspace_only", "filename": filename}


def _validate_file_auth(request: Request, token_param: str | None):
    """Validate auth via ?token= query param or Authorization header.

    Used for endpoints that serve files to <img>/<a> tags which can't send headers.
    """
    from fastapi import Query
    from enclave.webui.auth import validate_token

    if token_param:
        validate_token(token_param)
    else:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            validate_token(auth_header[7:])
        else:
            raise HTTPException(status_code=401, detail="Not authenticated")


@public_router.get("/{session_id}/file/{file_path:path}")
async def proxy_workspace_file(
    request: Request, session_id: str, file_path: str, token: str | None = None
):
    """Serve a file from the agent's workspace (for structured message images).

    The agent references paths as /workspace/... (the container mount point).
    We strip that prefix so the path is relative to the host workspace directory.
    Auth via ?token= query param so <img src="...?token=X"> works.
    """
    _validate_file_auth(request, token)

    import mimetypes
    from fastapi.responses import Response

    # Strip /workspace/ prefix — agent paths are container-relative
    if file_path.startswith("workspace/"):
        file_path = file_path[len("workspace/"):]

    config = request.app.state.config
    workspace_base = Path(config.container.workspace_base)
    # Resolve the workspace for this session
    workspace = workspace_base / session_id
    if not workspace.exists():
        # Try finding workspace by prefix match
        for d in workspace_base.iterdir():
            if d.is_dir() and d.name.startswith(session_id.split("-")[0]):
                workspace = d
                break

    # Resolve and sanitize — must stay within workspace. Use relative_to rather
    # than a string startswith, which would treat a sibling like
    # "<workspace>-evil" as inside the workspace (security review L1).
    resolved = (workspace / file_path).resolve()
    try:
        resolved.relative_to(workspace.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Path traversal denied")
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    content = resolved.read_bytes()
    ct = mimetypes.guess_type(str(resolved))[0] or "application/octet-stream"
    return Response(content=content, media_type=ct)


@public_router.get("/media/{server_name}/{media_id}")
async def proxy_matrix_media(
    request: Request, server_name: str, media_id: str, token: str | None = None
):
    """Proxy Matrix media (mxc://) so the browser can display images."""
    _validate_file_auth(request, token)

    config = _matrix_config(request)
    mat_token = await _get_matrix_token(config)

    # Use the authenticated media download endpoint
    url = f"{config.homeserver}/_matrix/client/v1/media/download/{server_name}/{media_id}"
    async with aiohttp.ClientSession() as session:
        headers = {"Authorization": f"Bearer {mat_token}"}
        async with session.get(url, headers=headers) as resp:
            if resp.status != 200:
                raise HTTPException(status_code=resp.status, detail="Media not found")
            content = await resp.read()
            ct = resp.headers.get("Content-Type", "application/octet-stream")
            from fastapi.responses import Response
            return Response(content=content, media_type=ct)


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

                    # Persistence is handled by the always-on EventPersister,
                    # not here — this stream only relays events to the browser
                    # for live display.
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
