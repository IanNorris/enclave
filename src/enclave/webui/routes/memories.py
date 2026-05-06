"""Mimir memory browser API routes.

Reads memory data via mimir-cli subprocess calls.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter()


# ─── Helpers ────────────────────────────────────────────────────────────────


def _mimir_config(request: Request):
    return request.app.state.config.mimir


def _canonical_log(request: Request) -> Path:
    cfg = _mimir_config(request)
    return Path(cfg.workspace_root) / cfg.agent_name / "canonical.log"


def _host_cli_bin(request: Request) -> str:
    """Get the host-side mimir-cli binary path."""
    cfg = _mimir_config(request)
    # Use the host librarian path directory to find mimir-cli
    librarian_dir = Path(cfg.host_librarian_bin).parent
    cli = librarian_dir / "mimir-cli"
    if cli.exists():
        return str(cli)
    # Fallback: look for it in PATH
    return "mimir-cli"


async def _run_mimir_cli(request: Request, *args: str, timeout: float = 30.0) -> str:
    """Run mimir-cli with given arguments and return stdout."""
    cli_bin = _host_cli_bin(request)
    log_path = str(_canonical_log(request))

    proc = await asyncio.create_subprocess_exec(
        cli_bin, *args, log_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)

    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace").strip()
        raise HTTPException(status_code=500, detail=f"mimir-cli failed: {err}")

    return stdout.decode("utf-8", errors="replace")


def _parse_log_output(output: str) -> dict[str, Any]:
    """Parse mimir-cli log output into structured records with resolved symbols."""
    symbols_map: dict[int, str] = {}
    memories: list[dict[str, Any]] = []
    checkpoints: list[dict[str, Any]] = []

    for line in output.split("\n"):
        line = line.strip()
        if not line:
            continue

        if line.startswith("SYMBOL_ALLOC"):
            id_match = re.search(r"id=SymbolId\((\d+)\)", line)
            name_match = re.search(r'name="([^"]*)"', line)
            if id_match and name_match:
                symbols_map[int(id_match.group(1))] = name_match.group(1)

        elif line.startswith("CHECKPOINT"):
            match = re.search(r"episode_id=SymbolId\((\d+)\)", line)
            at_match = re.search(r"at=(\S+)", line)
            count_match = re.search(r"memory_count=(\d+)", line)
            if match:
                checkpoints.append({
                    "type": "checkpoint",
                    "episode_id": int(match.group(1)),
                    "timestamp": at_match.group(1) if at_match else None,
                    "memory_count": int(count_match.group(1)) if count_match else 0,
                })

        elif line.startswith("SEM "):
            mem_match = re.search(r"memory_id=SymbolId\((\d+)\)", line)
            s_match = re.search(r"s=SymbolId\((\d+)\)", line)
            p_match = re.search(r"p=SymbolId\((\d+)\)", line)
            v_match = re.search(r"v=(.+)$", line)
            if mem_match:
                s_id = int(s_match.group(1)) if s_match else None
                p_id = int(p_match.group(1)) if p_match else None
                memories.append({
                    "type": "semantic",
                    "memory_id": int(mem_match.group(1)),
                    "subject_id": s_id,
                    "predicate_id": p_id,
                    "subject": symbols_map.get(s_id, f"#{s_id}") if s_id is not None else None,
                    "predicate": symbols_map.get(p_id, f"#{p_id}") if p_id is not None else None,
                    "value": v_match.group(1).strip() if v_match else None,
                })

        elif line.startswith("PRO "):
            mem_match = re.search(r"memory_id=SymbolId\((\d+)\)", line)
            rule_match = re.search(r"rule_id=SymbolId\((\d+)\)", line)
            if mem_match:
                rule_id = int(rule_match.group(1)) if rule_match else None
                memories.append({
                    "type": "procedural",
                    "memory_id": int(mem_match.group(1)),
                    "rule_id": rule_id,
                    "rule": symbols_map.get(rule_id, f"#{rule_id}") if rule_id is not None else None,
                })

    return {"memories": memories, "checkpoints": checkpoints, "symbols_map": symbols_map}


def _parse_symbols_output(output: str) -> list[dict[str, Any]]:
    """Parse mimir-cli symbols output."""
    symbols = []
    for line in output.split("\n"):
        line = line.strip()
        if not line:
            continue
        # SymbolId(X) name                   kind=Y
        match = re.match(r"SymbolId\((\d+)\)\s+(\S+)\s+kind=(\S+)", line)
        if match:
            symbols.append({
                "id": int(match.group(1)),
                "name": match.group(2),
                "kind": match.group(3),
            })
    return symbols


# ─── Endpoints ──────────────────────────────────────────────────────────────


@router.get("")
async def get_memories(request: Request):
    """Get all memory entries from the canonical log."""
    cfg = _mimir_config(request)
    if not cfg.enabled:
        raise HTTPException(status_code=503, detail="Mimir is not enabled")

    log_path = _canonical_log(request)
    if not log_path.exists():
        return {"records": [], "checkpoints": []}

    output = await _run_mimir_cli(request, "log")
    parsed = _parse_log_output(output)
    return {"records": parsed["memories"], "checkpoints": parsed["checkpoints"]}


@router.get("/symbols")
async def get_symbols(request: Request):
    """Get all symbols from the canonical log."""
    cfg = _mimir_config(request)
    if not cfg.enabled:
        raise HTTPException(status_code=503, detail="Mimir is not enabled")

    log_path = _canonical_log(request)
    if not log_path.exists():
        return {"symbols": []}

    output = await _run_mimir_cli(request, "symbols")
    symbols = _parse_symbols_output(output)
    return {"symbols": symbols}


@router.get("/stats")
async def get_stats(request: Request):
    """Get summary statistics about the memory corpus."""
    cfg = _mimir_config(request)
    if not cfg.enabled:
        raise HTTPException(status_code=503, detail="Mimir is not enabled")

    log_path = _canonical_log(request)
    drafts_dir = Path(cfg.workspace_root) / cfg.agent_name / "drafts"

    pending = 0
    accepted = 0
    failed = 0
    if (drafts_dir / "pending").exists():
        pending = len(list((drafts_dir / "pending").iterdir()))
    if (drafts_dir / "accepted").exists():
        accepted = len(list((drafts_dir / "accepted").iterdir()))
    if (drafts_dir / "failed").exists():
        failed = len(list((drafts_dir / "failed").iterdir()))

    # Count records via cli
    total_records = 0
    total_symbols = 0
    total_checkpoints = 0
    if log_path.exists():
        try:
            output = await _run_mimir_cli(request, "log")
            for line in output.split("\n"):
                if line.startswith("SEM ") or line.startswith("PRO ") or line.startswith("NAR "):
                    total_records += 1
                elif line.startswith("SYMBOL_ALLOC"):
                    total_symbols += 1
                elif line.startswith("CHECKPOINT"):
                    total_checkpoints += 1
        except Exception:
            pass

    return {
        "enabled": cfg.enabled,
        "records": total_records,
        "symbols": total_symbols,
        "checkpoints": total_checkpoints,
        "drafts": {"pending": pending, "accepted": accepted, "failed": failed},
        "log_path": str(log_path),
        "log_exists": log_path.exists(),
    }
