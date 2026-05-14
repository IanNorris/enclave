"""REST API routes for deferred (non-blocking) agent questions."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

log = logging.getLogger(__name__)
router = APIRouter(prefix="/asks", tags=["asks"])


def _get_store(request: Request):
    """Get the deferred asks store from app config."""
    from enclave.webui.deferred_asks import get_deferred_asks_store

    config = request.app.state.config
    workspace_base = Path(config.container.workspace_base)
    return get_deferred_asks_store(workspace_base)


def _control_sock_path(request: Request) -> Path:
    """Resolve the orchestrator control socket path."""
    config = request.app.state.config
    return Path(config.data_dir) / "control.sock"


async def _control_request(sock_path: Path, payload: dict, timeout: float = 5.0) -> dict | None:
    """Send a JSON request to the control socket and return the first response."""
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


async def _get_sessions(request: Request) -> dict[str, str]:
    """Get session id → name mapping from orchestrator."""
    sock_path = _control_sock_path(request)
    result = await _control_request(sock_path, {"action": "list"})
    if result and result.get("ok"):
        return {s["id"]: s.get("name", s["id"]) for s in result.get("sessions", [])}
    return {}


@router.get("")
async def list_asks(
    request: Request,
    session_id: str | None = None,
    status: str = "pending",
    limit: int = 50,
):
    """List deferred asks, optionally filtered by session and status."""
    store = _get_store(request)

    if status == "pending":
        asks = store.list_pending(session_id=session_id)
    else:
        asks = store.list_all(session_id=session_id, limit=limit)

    # Enrich with session names
    sessions = await _get_sessions(request)
    for ask in asks:
        ask["session_name"] = sessions.get(ask["session_id"], ask["session_id"])

    return {"asks": asks, "count": len(asks)}


@router.get("/count")
async def pending_count(request: Request, session_id: str | None = None):
    """Get the number of pending deferred asks."""
    store = _get_store(request)
    count = store.pending_count(session_id=session_id)
    return {"count": count}


class AnswerRequest(BaseModel):
    answer: str


@router.post("/{ask_id}/answer")
async def answer_ask(request: Request, ask_id: str, body: AnswerRequest):
    """Answer a deferred ask and deliver the response to the agent."""
    store = _get_store(request)
    ask = store.get(ask_id)
    if not ask:
        raise HTTPException(status_code=404, detail="Ask not found")
    if ask["status"] != "pending":
        raise HTTPException(status_code=400, detail=f"Ask already {ask['status']}")

    updated = store.answer(ask_id, body.answer)

    # Deliver the answer to the agent via control socket
    parts = [f'[Deferred answer] Re: "{ask["question"]}"']
    if ask.get("context"):
        parts.append(f"Context: {ask['context']}")
    parts.append(f"Answer: {body.answer}")
    message = "\n".join(parts)

    sock_path = _control_sock_path(request)
    result = await _control_request(
        sock_path,
        {"action": "send", "session": ask["session_id"], "content": message},
        timeout=10.0,
    )
    delivered = result is not None and result.get("ok", False)
    if not delivered:
        log.warning("Failed to deliver deferred answer to %s: %s", ask["session_id"], result)

    return {"ok": True, "ask": updated, "delivered": delivered}


@router.post("/{ask_id}/dismiss")
async def dismiss_ask(request: Request, ask_id: str):
    """Dismiss a deferred ask without answering."""
    store = _get_store(request)
    ask = store.get(ask_id)
    if not ask:
        raise HTTPException(status_code=404, detail="Ask not found")
    if ask["status"] != "pending":
        raise HTTPException(status_code=400, detail=f"Ask already {ask['status']}")

    success = store.dismiss(ask_id)
    return {"ok": success}
