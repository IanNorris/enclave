"""Scheduling API routes.

Exposes the orchestrator's recurring schedules and one-shot timers so they can
be viewed, created, and cancelled from the Web UI. Schedules can target an
existing session, the always-on concierge, or spawn a fresh worker session each
time they fire.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter()


def _config(request: Request):
    return request.app.state.config


def _control_socket(request: Request) -> Path:
    return Path(_config(request).data_dir) / "control.sock"


async def _control_request(
    request: Request, payload: dict[str, Any], timeout: float = 10.0,
) -> dict[str, Any]:
    """Send a one-shot request to the orchestrator control socket."""
    sock_path = _control_socket(request)
    if not sock_path.exists():
        raise HTTPException(status_code=503, detail="Orchestrator not running")
    try:
        reader, writer = await asyncio.open_unix_connection(str(sock_path))
        writer.write(json.dumps(payload).encode() + b"\n")
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=timeout)
        writer.close()
        await writer.wait_closed()
    except (OSError, asyncio.TimeoutError) as e:
        raise HTTPException(status_code=504, detail=f"Control socket error: {e}")
    if not line:
        raise HTTPException(status_code=502, detail="Empty response from orchestrator")
    try:
        return json.loads(line.decode())
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=502, detail=f"Invalid response: {e}")


class ScheduleCreate(BaseModel):
    target: str = "session"  # "session" | "concierge" | "spawn"
    session_id: str = ""
    reason: str = ""
    interval_seconds: int = 3600
    spawn_brief: str = ""


@router.get("")
async def list_schedules(request: Request):
    """Return all recurring schedules and one-shot timers."""
    resp = await _control_request(request, {"action": "schedule_list"})
    if not resp.get("ok"):
        raise HTTPException(status_code=502, detail=resp.get("error", "Failed to list schedules"))
    return {
        "schedules": resp.get("schedules", []),
        "timers": resp.get("timers", []),
    }


@router.post("")
async def create_schedule(request: Request, body: ScheduleCreate):
    """Create a recurring schedule."""
    payload = {
        "action": "schedule_add",
        "target": body.target,
        "session_id": body.session_id,
        "reason": body.reason,
        "interval_seconds": body.interval_seconds,
        "spawn_brief": body.spawn_brief,
    }
    resp = await _control_request(request, payload)
    if not resp.get("ok"):
        raise HTTPException(status_code=400, detail=resp.get("error", "Failed to create schedule"))
    return {"id": resp.get("id", ""), "next_fire": resp.get("next_fire", 0)}


@router.delete("/{schedule_id}")
async def cancel_schedule(request: Request, schedule_id: str):
    """Cancel a recurring schedule or timer by id."""
    resp = await _control_request(
        request, {"action": "schedule_cancel", "id": schedule_id},
    )
    if not resp.get("ok"):
        raise HTTPException(status_code=404, detail="Schedule not found")
    return {"cancelled": True, "id": schedule_id}
