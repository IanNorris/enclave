"""Consult-panel configuration API routes.

Exposes the data-driven ``consult_panel`` roster so it can be viewed and
edited from the Web UI: per-panelist prompt (voice/focus), model preference
list, enabled toggle, and user-defined members. The canonical definition is
stored on the orchestrator host (outside the repo), so private/preview model
ids entered here never reach source control.
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


class PanelMember(BaseModel):
    id: str = ""
    name: str = ""
    voice: str = ""
    focus: str = ""
    models: list[str] = []
    enabled: bool = True


class PanelUpdate(BaseModel):
    members: list[PanelMember]


@router.get("")
async def get_panel(request: Request):
    """Return the current consult_panel roster."""
    resp = await _control_request(request, {"action": "panel_get"})
    if not resp.get("ok"):
        raise HTTPException(status_code=502, detail=resp.get("error", "Failed to load panel"))
    return resp.get("panel", {"version": 1, "members": []})


@router.put("")
async def update_panel(request: Request, body: PanelUpdate):
    """Persist a new consult_panel roster and push it to active sessions."""
    members = [m.model_dump() for m in body.members]
    resp = await _control_request(
        request, {"action": "panel_set", "panel": {"members": members}},
    )
    if not resp.get("ok"):
        raise HTTPException(status_code=502, detail=resp.get("error", "Failed to save panel"))
    return {"panel": resp.get("panel", {}), "pushed": resp.get("pushed", 0)}


@router.get("/models")
async def known_models(request: Request):
    """Aggregate model ids the agents have reported as available.

    The agent writes ``.enclave-models.json`` into each workspace at startup.
    These power autocomplete suggestions in the editor; the field still
    accepts free text so private/preview model ids can be used without ever
    being committed to the repo.
    """
    ws_base = Path(_config(request).container.workspace_base)
    ids: set[str] = set()
    if ws_base.exists():
        for entry in ws_base.iterdir():
            models_file = entry / ".enclave-models.json"
            if not models_file.is_file():
                continue
            try:
                data = json.loads(models_file.read_text())
            except (OSError, ValueError):
                continue
            for mid in data.get("available", []) or []:
                if isinstance(mid, str) and mid:
                    ids.add(mid)
    return {"models": sorted(ids)}
