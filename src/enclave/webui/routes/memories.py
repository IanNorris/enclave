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


def _parse_decode_output(output: str) -> dict[str, Any]:
    """Parse mimir-cli decode output (S-expressions) into structured records."""
    memories: list[dict[str, Any]] = []
    checkpoints: list[dict[str, Any]] = []

    for line in output.split("\n"):
        line = line.strip()
        if not line or not line.startswith("("):
            continue

        if line.startswith("(sem "):
            mem = _parse_sem(line)
            if mem:
                memories.append(mem)
        elif line.startswith("(pro "):
            mem = _parse_pro(line)
            if mem:
                memories.append(mem)
        elif line.startswith("(checkpoint "):
            cp = _parse_checkpoint_sexp(line)
            if cp:
                checkpoints.append(cp)

    return {"memories": memories, "checkpoints": checkpoints}


def _parse_sem(line: str) -> dict[str, Any] | None:
    """Parse a semantic memory S-expression.

    Formats:
      (sem @subject @predicate "value" :src @source :c 0.9 :v 2026-04-08T...)
      (sem @subject @predicate @symbol :src @source :c 0.9 :v 2026-04-08T...)
      (sem @subject @predicate true :src @source :c 0.9 :v 2026-04-08T...)
    """
    # Strip outer parens
    inner = line[1:-1].strip() if line.endswith(")") else line[1:].strip()
    if not inner.startswith("sem "):
        return None

    # Tokenize respecting quoted strings
    tokens = _tokenize_sexp(inner)
    if len(tokens) < 4:
        return None

    # tokens[0] = "sem", tokens[1] = subject, tokens[2] = predicate, tokens[3] = object/value
    subject = _clean_symbol(tokens[1])
    predicate = _clean_symbol(tokens[2])
    obj_value = _clean_symbol(tokens[3])

    # Extract keyword args
    kwargs = _extract_kwargs(tokens[4:])

    return {
        "type": "semantic",
        "subject": subject,
        "predicate": predicate,
        "object": obj_value,
        "source": kwargs.get("src"),
        "confidence": kwargs.get("c"),
        "timestamp": kwargs.get("v"),
    }


def _parse_pro(line: str) -> dict[str, Any] | None:
    """Parse a procedural memory S-expression.

    Format: (pro @rule "condition" "action" :scp @scope :src @source :c 0.95)
    """
    inner = line[1:-1].strip() if line.endswith(")") else line[1:].strip()
    if not inner.startswith("pro "):
        return None

    tokens = _tokenize_sexp(inner)
    if len(tokens) < 4:
        return None

    rule = _clean_symbol(tokens[1])
    condition = _clean_symbol(tokens[2])
    action = _clean_symbol(tokens[3])

    kwargs = _extract_kwargs(tokens[4:])

    return {
        "type": "procedural",
        "rule": rule,
        "condition": condition,
        "action": action,
        "scope": kwargs.get("scp"),
        "source": kwargs.get("src"),
        "confidence": kwargs.get("c"),
    }


def _parse_checkpoint_sexp(line: str) -> dict[str, Any] | None:
    """Parse checkpoint S-expression if present."""
    inner = line[1:-1].strip() if line.endswith(")") else line[1:].strip()
    tokens = _tokenize_sexp(inner)
    kwargs = _extract_kwargs(tokens[1:])
    return {
        "type": "checkpoint",
        "episode": kwargs.get("episode"),
        "timestamp": kwargs.get("at"),
        "memory_count": kwargs.get("n"),
    }


def _tokenize_sexp(s: str) -> list[str]:
    """Tokenize an S-expression body, respecting quoted strings."""
    tokens = []
    i = 0
    while i < len(s):
        if s[i].isspace():
            i += 1
            continue
        if s[i] == '"':
            # Quoted string
            j = i + 1
            while j < len(s) and s[j] != '"':
                if s[j] == '\\':
                    j += 1
                j += 1
            tokens.append(s[i + 1:j])
            i = j + 1
        else:
            # Bare token
            j = i
            while j < len(s) and not s[j].isspace():
                j += 1
            tokens.append(s[i:j])
            i = j
    return tokens


def _clean_symbol(token: str) -> str:
    """Strip leading @ from symbol names."""
    return token[1:] if token.startswith("@") else token


def _extract_kwargs(tokens: list[str]) -> dict[str, str | None]:
    """Extract :key value pairs from token list."""
    result: dict[str, str | None] = {}
    i = 0
    while i < len(tokens):
        if tokens[i].startswith(":") and i + 1 < len(tokens):
            key = tokens[i][1:]
            val = _clean_symbol(tokens[i + 1])
            # Try to parse confidence as float
            if key == "c":
                try:
                    val = str(round(float(val), 3))
                except ValueError:
                    pass
            result[key] = val
            i += 2
        else:
            i += 1
    return result


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

    output = await _run_mimir_cli(request, "decode")
    parsed = _parse_decode_output(output)
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
            output = await _run_mimir_cli(request, "decode")
            for line in output.split("\n"):
                line = line.strip()
                if line.startswith("(sem ") or line.startswith("(pro ") or line.startswith("(nar "):
                    total_records += 1
                elif line.startswith("(checkpoint"):
                    total_checkpoints += 1
            # Get symbol count from symbols subcommand
            sym_output = await _run_mimir_cli(request, "symbols")
            for sline in sym_output.split("\n"):
                if sline.strip() and re.match(r"SymbolId\(\d+\)", sline.strip()):
                    total_symbols += 1
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
