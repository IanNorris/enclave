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
from enclave.common.openspec_log import (
    read_log, write_log_atomic, append_event, snapshot_files, content_hash,
)

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
    return read_log(change_dir)


def _write_log_atomic(change_dir: Path, doc: dict) -> None:
    write_log_atomic(change_dir, doc)


def _append_event(change_dir: Path, event: dict, blobs: dict[str, str] | None = None) -> dict:
    return append_event(change_dir, event, blobs)


def _hash(content: str) -> str:
    return content_hash(content)


def _snapshot_files(change_dir: Path, root: Path) -> tuple[dict[str, str], dict[str, str]]:
    return snapshot_files(change_dir, root)


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
    """List active OpenSpec changes (excludes the archive), most-recently-edited
    first so the change the user is actively working on is at the top."""
    changes_dir = _changes_dir(session_id)
    if not changes_dir.is_dir():
        return {"exists": False, "changes": []}
    changes = []
    for entry in sorted(changes_dir.iterdir()):
        if not entry.is_dir() or entry.name == "archive":
            continue
        changes.append((_change_mtime(entry), _change_summary(session_id, entry)))
    # Newest edit first; the review-log workflow file is ignored by _change_mtime
    # so approving/commenting doesn't reorder the list.
    changes.sort(key=lambda t: t[0], reverse=True)
    return {"exists": True, "changes": [c for _, c in changes]}


def _change_mtime(change_dir: Path) -> float:
    """Most recent mtime among a change's spec markdown files.

    Only the spec content (``*.md``) counts — the ``.enclave-review.json``
    workflow file is deliberately excluded so a review action does not bump a
    change to the top of the list.
    """
    latest = 0.0
    for p in change_dir.rglob("*.md"):
        try:
            latest = max(latest, p.stat().st_mtime)
        except OSError:
            continue
    return latest


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
    snapshot, blobs = _snapshot_files(change_dir, _openspec_root(session_id))
    event = {
        "id": f"rev_{int(time.time() * 1000)}",
        "type": "review",
        "at": now,
        "by": "ian",
        "state": body.state,
        "overall_note": body.note,
        "comments": [c.model_dump() for c in body.comments],
        "snapshot_at_review": snapshot,
    }
    _append_event(change_dir, event, blobs)
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


@router.get("/{session_id}/openspec/changes/{name}/diff")
async def get_diff(request: Request, session_id: str, name: str):
    """Changed source lines per file since the user's last review.

    Baseline = the latest review's ``snapshot_at_review`` content; "after" = the
    current file content (what the UI renders). Changed line numbers are 0-based
    and relative to the CURRENT version, so they map directly onto the rendered
    ``data-line`` blocks. Returns ``{path: [changed_line, ...]}``.

    Lazily backfills a missing baseline blob from the current file when the hash
    still matches (covers reviews recorded before blobs were stored).
    """
    import difflib

    change_dir = _safe_change_dir(session_id, name)
    if not change_dir.is_dir():
        raise HTTPException(status_code=404, detail="Change not found")
    root = _openspec_root(session_id)
    doc = _read_log(change_dir)
    events = doc.get("events", [])
    blobs = doc.get("blobs", {})

    reviews = [e for e in events if e.get("type") == "review"]
    if not reviews:
        return {"changed": {}}
    baseline = sorted(reviews, key=lambda e: e.get("at", ""))[-1]
    snap = baseline.get("snapshot_at_review", {}) or {}

    changed: dict[str, list[int]] = {}
    backfilled = False
    for rel, base_hash in snap.items():
        current = _read_text(root / rel)
        if current is None:
            continue
        # Backfill baseline content if we only have the hash and the file is
        # still unchanged since the review (hash matches current content).
        base_content = blobs.get(base_hash)
        if base_content is None:
            if _hash(current) == base_hash:
                blobs[base_hash] = current
                base_content = current
                backfilled = True
            else:
                continue  # can't reconstruct baseline; skip this file
        if base_content == current:
            continue
        lines = _changed_after_lines(base_content, current, difflib)
        if lines:
            changed[rel] = lines
    if backfilled:
        doc["blobs"] = blobs
        _write_log_atomic(change_dir, doc)
    return {"changed": changed}


def _changed_after_lines(before: str, after: str, difflib) -> list[int]:
    """0-based line numbers in ``after`` that differ from ``before``.

    Uses SequenceMatcher opcodes; 'replace'/'insert' mark the after-range, and a
    'delete' marks the line where content was removed so the surrounding block is
    still flagged. Trailing whitespace is normalized so pure reflow doesn't flag.
    """
    b = [ln.rstrip() for ln in before.split("\n")]
    a = [ln.rstrip() for ln in after.split("\n")]
    sm = difflib.SequenceMatcher(a=b, b=a, autojunk=False)
    out: set[int] = set()
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("replace", "insert"):
            out.update(range(j1, j2))
        elif tag == "delete":
            out.add(min(j1, max(len(a) - 1, 0)))
    return sorted(out)


class RevisionBody(BaseModel):
    summary: str = ""
    why: str = ""
    in_response_to: str = ""
    related_comment_ids: list[str] = []
    files_changed: list[str] = []
    resolutions: list[dict] = []


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
    snapshot, blobs = _snapshot_files(change_dir, _openspec_root(session_id))
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
        "resolutions": body.resolutions,
        "snapshot_after": snapshot,
    }
    _append_event(change_dir, event, blobs)
    return {"ok": True, "revision_id": event["id"], "state": derive_state(_read_log(change_dir)["events"])}

