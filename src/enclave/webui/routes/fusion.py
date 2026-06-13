"""Fusion configuration API routes.

Exposes the data-driven Fusion config (compound-model presets plus the Auto
Fusion routing settings) so it can be viewed and edited from the Web UI:
per-preset participants/judge/synthesizer model lists, the Auto Fusion base
model, and the complexity escalation threshold. The canonical definition is
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


class FusionPreset(BaseModel):
    id: str = ""
    name: str = ""
    description: str = ""
    participants: list[list[str]] = []
    judge: list[str] = []
    synthesizer: list[str] = []
    enabled: bool = True


class FusionUpdate(BaseModel):
    presets: list[FusionPreset]
    base_model: str = ""
    auto_threshold: int = 4


@router.get("")
async def get_fusion(request: Request):
    """Return the current Fusion config (presets + auto routing)."""
    resp = await _control_request(request, {"action": "fusion_get"})
    if not resp.get("ok"):
        raise HTTPException(status_code=502, detail=resp.get("error", "Failed to load fusion"))
    return resp.get("fusion", {"version": 1, "presets": [], "base_model": "", "auto_threshold": 4})


@router.put("")
async def update_fusion(request: Request, body: FusionUpdate):
    """Persist a new Fusion config and push it to active sessions."""
    doc = {
        "presets": [p.model_dump() for p in body.presets],
        "base_model": body.base_model,
        "auto_threshold": body.auto_threshold,
    }
    resp = await _control_request(request, {"action": "fusion_set", "fusion": doc})
    if not resp.get("ok"):
        raise HTTPException(status_code=502, detail=resp.get("error", "Failed to save fusion"))
    return {"fusion": resp.get("fusion", {}), "pushed": resp.get("pushed", 0)}


@router.get("/models")
async def known_models(request: Request):
    """Aggregate model ids the agents have reported as available.

    Powers autocomplete suggestions in the editor; the field still accepts
    free text so private/preview model ids can be used without ever being
    committed to the repo.
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
