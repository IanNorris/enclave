"""REST API routes for deferred (non-blocking) agent questions."""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

router = APIRouter(prefix="/asks", tags=["asks"])


def _get_store(request: Request):
    """Get the deferred asks store from app config."""
    from enclave.webui.deferred_asks import get_deferred_asks_store

    config = request.app.state.config
    workspace_base = Path(config.container.workspace_base)
    return get_deferred_asks_store(workspace_base)


def _get_sessions(request: Request) -> dict[str, str]:
    """Get session id → name mapping from orchestrator."""
    config = request.app.state.config
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(2.0)
        sock.connect(config.orchestrator.control_socket)
        sock.sendall(json.dumps({"action": "list"}).encode() + b"\n")
        data = sock.recv(65536)
        sock.close()
        result = json.loads(data.decode())
        if result.get("ok"):
            return {s["id"]: s.get("name", s["id"]) for s in result.get("sessions", [])}
    except Exception:
        pass
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
    sessions = _get_sessions(request)
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
    config = request.app.state.config
    try:
        # Build a contextual message so the agent can resume
        parts = [f'[Deferred answer] Re: "{ask["question"]}"']
        if ask.get("context"):
            parts.append(f"Context: {ask['context']}")
        parts.append(f"Answer: {body.answer}")
        message = "\n".join(parts)

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(5.0)
        sock.connect(config.orchestrator.control_socket)
        sock.sendall(
            json.dumps({
                "action": "send",
                "session": ask["session_id"],
                "content": message,
            }).encode()
            + b"\n"
        )
        # Read response
        data = sock.recv(65536)
        sock.close()
    except Exception as e:
        # Answer is saved even if delivery fails
        pass

    return {"ok": True, "ask": updated}


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
