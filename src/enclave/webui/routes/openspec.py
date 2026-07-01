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

import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from enclave.webui.routes.review_state import derive_state, derive_comment_statuses

router = APIRouter()

# Repo root = four parents up from this file:
# src/enclave/webui/routes/openspec.py -> <repo>/
_REPO_ROOT = Path(__file__).resolve().parents[4]

_CHECK_DONE = re.compile(r"^\s*[-*]\s+\[[xX]\]\s+(.*)$")
_CHECK_OPEN = re.compile(r"^\s*[-*]\s+\[\s\]\s+(.*)$")

_REVIEW_FILE = ".enclave-review.json"


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


def _read_log(change_dir: Path) -> dict:
    """Load the append-only review log, tolerating absence/corruption."""
    raw = _read_text(change_dir / _REVIEW_FILE)
    if not raw:
        return {"version": 1, "events": []}
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError:
        return {"version": 1, "events": []}
    if "events" not in doc or not isinstance(doc["events"], list):
        doc["events"] = []
    return doc


def _write_log_atomic(change_dir: Path, doc: dict) -> None:
    """Write the log via temp-file + rename so a concurrent read never sees a
    half-written file."""
    target = change_dir / _REVIEW_FILE
    tmp = change_dir / f".{_REVIEW_FILE}.{os.getpid()}.{int(time.time()*1000)}.tmp"
    tmp.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    os.replace(str(tmp), str(target))


def _append_event(change_dir: Path, event: dict) -> dict:
    """Append one event to the log (idempotent on event id) and return the doc."""
    doc = _read_log(change_dir)
    if event.get("id") and any(e.get("id") == event["id"] for e in doc["events"]):
        return doc  # idempotent: duplicate append is a no-op
    doc["events"].append(event)
    # Keep the top-level badge fields in sync for the change list (latest review).
    if event.get("type") == "review":
        doc["state"] = event.get("state")
        doc["by"] = event.get("by")
        doc["at"] = event.get("at")
    _write_log_atomic(change_dir, doc)
    return doc


def _snapshot_files(change_dir: Path, root: Path) -> dict[str, str]:
    """Content-hash snapshot of a change's markdown files (store-by-hash)."""
    snap: dict[str, str] = {}
    for p in sorted(change_dir.rglob("*.md")):
        content = _read_text(p)
        if content is None:
            continue
        rel = str(p.relative_to(root))
        snap[rel] = "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
    return snap


def _review_state(change_dir: Path) -> dict | None:
    """Return a compact review badge {state, note, at} derived from the log."""
    doc = _read_log(change_dir)
    events = doc.get("events", [])
    if not events:
        # Back-compat: a legacy flat record (pre-event-log) may still carry state.
        legacy = doc if doc.get("state") else None
        return {"state": legacy["state"], "at": legacy.get("at")} if legacy else None
    return {"state": derive_state(events), "at": doc.get("at")}



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


class ReviewComment(BaseModel):
    id: str
    section: str = ""
    path: str = ""
    start_line: int = 0
    end_line: int = 0
    block_text: str = ""
    block_hash: str = ""
    comment: str = ""


class ReviewBody(BaseModel):
    state: str  # "approved" | "changes_requested" | "commented"
    note: str = ""
    comments: list[ReviewComment] = []


@router.post("/{session_id}/openspec/changes/{name}/review")
async def post_review(request: Request, session_id: str, name: str, body: ReviewBody):
    """Append a review event to the change's append-only log.

    The frontend separately sends the agent a tagged chat message; this endpoint
    persists the durable review event (with inline comments) so state + history
    survive reloads and are visible to the agent.
    """
    if body.state not in ("approved", "changes_requested", "commented"):
        raise HTTPException(status_code=400, detail="Invalid review state")
    change_dir = _safe_change_dir(session_id, name)
    if not change_dir.is_dir():
        raise HTTPException(status_code=404, detail="Change not found")

    now = datetime.now(timezone.utc).isoformat()
    event = {
        "id": f"rev_{int(time.time() * 1000)}",
        "type": "review",
        "at": now,
        "by": "ian",
        "state": body.state,
        "overall_note": body.note,
        "comments": [c.model_dump() for c in body.comments],
        "snapshot_at_review": _snapshot_files(change_dir, _openspec_root(session_id)),
    }
    _append_event(change_dir, event)
    return {"ok": True, "review_id": event["id"], "state": derive_state(_read_log(change_dir)["events"])}


@router.get("/{session_id}/openspec/changes/{name}/state")
async def get_state(request: Request, session_id: str, name: str):
    """Derived review state + per-comment status for a change."""
    change_dir = _safe_change_dir(session_id, name)
    if not change_dir.is_dir():
        raise HTTPException(status_code=404, detail="Change not found")
    events = _read_log(change_dir).get("events", [])
    statuses = derive_comment_statuses(events)
    return {
        "state": derive_state(events),
        "comment_statuses": statuses,
        "events": events,
    }


class RevisionBody(BaseModel):
    summary: str = ""
    why: str = ""
    in_response_to: str = ""
    related_comment_ids: list[str] = []
    files_changed: list[str] = []


@router.post("/{session_id}/openspec/changes/{name}/revision-log")
async def post_revision(request: Request, session_id: str, name: str, body: RevisionBody):
    """Record an agent_revision event (the 'why' a spec changed).

    Called by the agent (via the openspec_revision_log tool) after applying
    review feedback. The handler snapshots the current files itself — it never
    trusts caller-supplied content.
    """
    change_dir = _safe_change_dir(session_id, name)
    if not change_dir.is_dir():
        raise HTTPException(status_code=404, detail="Change not found")
    now = datetime.now(timezone.utc).isoformat()
    event = {
        "id": f"arev_{int(time.time() * 1000)}",
        "type": "agent_revision",
        "at": now,
        "by": "agent",
        "in_response_to": body.in_response_to or None,
        "summary": body.summary,
        "why": body.why,
        "related_comment_ids": body.related_comment_ids,
        "files_changed": body.files_changed,
        "snapshot_after": _snapshot_files(change_dir, _openspec_root(session_id)),
    }
    _append_event(change_dir, event)
    return {"ok": True, "revision_id": event["id"], "state": derive_state(_read_log(change_dir)["events"])}

