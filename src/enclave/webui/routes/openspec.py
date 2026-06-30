"""OpenSpec review API — read-only access to the repo's ``openspec/`` changes
plus a small durable review record.

Phase A (dogfood): the read-root is the Enclave repo itself, so the Specs UI
shows the changes that drive Enclave's own development. The read-root is a
single resolver (`_openspec_root`) so switching to per-session workspaces
(Phase B) later is a one-function change.

The web process never invokes the ``openspec`` CLI — it reads the markdown
directly. Scaffolding/validate/archive remain the agent's job via the CLI.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter()

# Repo root = four parents up from this file:
# src/enclave/webui/routes/openspec.py -> <repo>/
_REPO_ROOT = Path(__file__).resolve().parents[4]

_CHECK_DONE = re.compile(r"^\s*[-*]\s+\[[xX]\]\s+(.*)$")
_CHECK_OPEN = re.compile(r"^\s*[-*]\s+\[\s\]\s+(.*)$")


def _openspec_root(session_id: str) -> Path:
    """Resolve the directory whose ``openspec/`` is read for this session.

    Phase A: always the Enclave repo root (overridable via
    ``ENCLAVE_OPENSPEC_ROOT``). Phase B would return the session workspace.
    ``session_id`` is accepted now for URL symmetry and a future swap.
    """
    override = os.environ.get("ENCLAVE_OPENSPEC_ROOT")
    return Path(override) if override else _REPO_ROOT


def _changes_dir(session_id: str) -> Path:
    return _openspec_root(session_id) / "openspec" / "changes"


def _safe_change_dir(session_id: str, name: str) -> Path:
    """Resolve a change directory, rejecting traversal outside changes/."""
    base = _changes_dir(session_id).resolve()
    target = (base / name).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        raise HTTPException(status_code=403, detail="Path traversal denied")
    return target


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _task_progress(tasks_md: str | None) -> dict:
    """Parse markdown checkboxes into progress + current/next task labels."""
    done = 0
    open_tasks: list[str] = []
    if tasks_md:
        for line in tasks_md.splitlines():
            if _CHECK_DONE.match(line):
                done += 1
            else:
                m = _CHECK_OPEN.match(line)
                if m:
                    open_tasks.append(m.group(1).strip())
    total = done + len(open_tasks)
    return {
        "done": done,
        "total": total,
        "percent": round(done / total * 100) if total else 0,
        "current": open_tasks[0] if open_tasks else None,
        "next": open_tasks[1] if len(open_tasks) > 1 else None,
    }


def _review_state(change_dir: Path) -> dict | None:
    raw = _read_text(change_dir / ".enclave-review.json")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _change_summary(session_id: str, change_dir: Path) -> dict:
    name = change_dir.name
    rel = lambda p: str(p.relative_to(_openspec_root(session_id)))  # noqa: E731
    proposal = change_dir / "proposal.md"
    design = change_dir / "design.md"
    tasks = change_dir / "tasks.md"
    spec_paths = sorted(
        rel(p) for p in (change_dir / "specs").rglob("*.md")
    ) if (change_dir / "specs").is_dir() else []
    return {
        "id": name,
        "proposalPath": rel(proposal) if proposal.is_file() else None,
        "designPath": rel(design) if design.is_file() else None,
        "tasksPath": rel(tasks) if tasks.is_file() else None,
        "specPaths": spec_paths,
        "taskProgress": _task_progress(_read_text(tasks)),
        "review": _review_state(change_dir),
    }


@router.get("/{session_id}/openspec/changes")
async def list_changes(request: Request, session_id: str):
    """List active OpenSpec changes (excludes the archive)."""
    changes_dir = _changes_dir(session_id)
    if not changes_dir.is_dir():
        return {"exists": False, "changes": []}
    changes = []
    for entry in sorted(changes_dir.iterdir()):
        if not entry.is_dir() or entry.name == "archive":
            continue
        changes.append(_change_summary(session_id, entry))
    return {"exists": True, "changes": changes}


@router.get("/{session_id}/openspec/changes/{name}")
async def get_change(request: Request, session_id: str, name: str):
    """Return the markdown artifacts + specs map for one change."""
    change_dir = _safe_change_dir(session_id, name)
    if not change_dir.is_dir():
        raise HTTPException(status_code=404, detail="Change not found")
    specs: dict[str, str] = {}
    specs_dir = change_dir / "specs"
    if specs_dir.is_dir():
        for p in sorted(specs_dir.rglob("*.md")):
            content = _read_text(p)
            if content is not None:
                specs[str(p.relative_to(change_dir))] = content
    return {
        "id": name,
        "proposal": _read_text(change_dir / "proposal.md"),
        "design": _read_text(change_dir / "design.md"),
        "tasks": _read_text(change_dir / "tasks.md"),
        "specs": specs,
        "taskProgress": _task_progress(_read_text(change_dir / "tasks.md")),
        "review": _review_state(change_dir),
    }


class ReviewBody(BaseModel):
    state: str  # "approved" | "changes_requested" | "commented"
    note: str = ""


@router.post("/{session_id}/openspec/changes/{name}/review")
async def post_review(request: Request, session_id: str, name: str, body: ReviewBody):
    """Persist a durable review record for a change.

    The frontend separately sends the agent a tagged chat message; this endpoint
    only writes the badge so it survives reloads and is visible to the agent.
    """
    if body.state not in ("approved", "changes_requested", "commented"):
        raise HTTPException(status_code=400, detail="Invalid review state")
    change_dir = _safe_change_dir(session_id, name)
    if not change_dir.is_dir():
        raise HTTPException(status_code=404, detail="Change not found")
    record = {
        "state": body.state,
        "note": body.note,
        "by": "user",
        "at": datetime.now(timezone.utc).isoformat(),
    }
    (change_dir / ".enclave-review.json").write_text(
        json.dumps(record, indent=2), encoding="utf-8"
    )
    return record
