"""Bug management API routes.

Reads/writes per-project .enclave-bugs/ markdown files (source of truth).
"""

from __future__ import annotations

import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiofiles
from fastapi import APIRouter, HTTPException, Request, UploadFile, File
from pydantic import BaseModel

router = APIRouter()


# ─── Helpers ────────────────────────────────────────────────────────────────


def _workspace_base(request: Request) -> Path:
    return Path(request.app.state.config.container.workspace_base)


def _discover_projects(request: Request) -> list[dict[str, str]]:
    """Find all projects that have .enclave-bugs/ directories."""
    ws_base = _workspace_base(request)
    projects = []
    if not ws_base.exists():
        return projects

    for session_dir in sorted(ws_base.iterdir()):
        if not session_dir.is_dir():
            continue
        # Look for .enclave-bugs in any subdirectory (project workspace)
        for root, dirs, _ in os.walk(session_dir):
            dirs[:] = [d for d in dirs if not d.startswith(".") or d == ".enclave-bugs"]
            bugs_dir = Path(root) / ".enclave-bugs"
            if bugs_dir.is_dir():
                # Project name is the relative path from workspace base
                rel = Path(root).relative_to(ws_base)
                projects.append({
                    "session": session_dir.name,
                    "path": str(rel),
                    "bugs_dir": str(bugs_dir),
                    "name": Path(root).name,
                })
    return projects


def _parse_bug_file(filepath: Path) -> dict[str, Any] | None:
    """Parse a bug markdown file with YAML frontmatter."""
    try:
        text = filepath.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    # Extract YAML frontmatter
    frontmatter = {}
    body = text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            import yaml
            try:
                frontmatter = yaml.safe_load(parts[1]) or {}
            except Exception:
                pass
            body = parts[2].strip()

    return {
        "id": frontmatter.get("id", filepath.stem),
        "title": frontmatter.get("title", filepath.stem),
        "status": frontmatter.get("status", "open"),
        "severity": frontmatter.get("severity", "medium"),
        "created": frontmatter.get("created", ""),
        "updated": frontmatter.get("updated", ""),
        "body": body,
        "file": filepath.name,
    }


def _serialize_bug(bug: dict[str, Any]) -> str:
    """Serialize a bug dict back to markdown with YAML frontmatter."""
    import yaml

    fm = {
        "id": bug["id"],
        "title": bug["title"],
        "status": bug["status"],
        "severity": bug["severity"],
        "created": bug.get("created", datetime.now(timezone.utc).isoformat()),
        "updated": datetime.now(timezone.utc).isoformat(),
    }
    body = bug.get("body", "")
    return f"---\n{yaml.dump(fm, default_flow_style=False)}---\n\n{body}\n"


# ─── Models ─────────────────────────────────────────────────────────────────


class BugCreate(BaseModel):
    title: str
    severity: str = "medium"
    body: str = ""


class BugUpdate(BaseModel):
    title: str | None = None
    status: str | None = None
    severity: str | None = None
    body: str | None = None


# ─── Project Listing ────────────────────────────────────────────────────────


@router.get("/projects")
async def list_projects(request: Request):
    """List all projects that have bug directories."""
    return _discover_projects(request)


# ─── Bug CRUD ───────────────────────────────────────────────────────────────


@router.get("/{session_id}")
async def list_bugs(request: Request, session_id: str):
    """List all bugs for a session."""
    ws_base = _workspace_base(request)
    session_dir = ws_base / session_id
    if not session_dir.exists():
        raise HTTPException(status_code=404, detail="Session not found")

    bugs = []
    for bugs_dir in session_dir.rglob(".enclave-bugs"):
        if not bugs_dir.is_dir():
            continue
        for f in sorted(bugs_dir.iterdir()):
            if f.suffix == ".md" and not f.name.startswith("."):
                bug = _parse_bug_file(f)
                if bug:
                    bug["project"] = str(bugs_dir.parent.relative_to(session_dir))
                    bugs.append(bug)
    return bugs


@router.get("/{session_id}/{bug_id}")
async def get_bug(request: Request, session_id: str, bug_id: str):
    """Get a specific bug by ID."""
    ws_base = _workspace_base(request)
    session_dir = ws_base / session_id
    if not session_dir.exists():
        raise HTTPException(status_code=404, detail="Session not found")

    # Search for the bug file
    for bugs_dir in session_dir.rglob(".enclave-bugs"):
        target = bugs_dir / f"{bug_id}.md"
        if target.exists():
            bug = _parse_bug_file(target)
            if bug:
                bug["project"] = str(bugs_dir.parent.relative_to(session_dir))
                return bug

    raise HTTPException(status_code=404, detail=f"Bug {bug_id} not found")


@router.post("/{session_id}/{project_path:path}/create")
async def create_bug(request: Request, session_id: str, project_path: str, body: BugCreate):
    """Create a new bug in a project."""
    ws_base = _workspace_base(request)
    project_dir = ws_base / session_id / project_path
    if not project_dir.exists():
        raise HTTPException(status_code=404, detail="Project directory not found")

    bugs_dir = project_dir / ".enclave-bugs"
    bugs_dir.mkdir(parents=True, exist_ok=True)

    # Generate next ID
    prefix = "".join(c for c in project_dir.name[:3].upper() if c.isalpha()) or "BUG"
    existing = [f.stem for f in bugs_dir.iterdir() if f.suffix == ".md"]
    nums = [int(re.search(r"(\d+)$", e).group(1)) for e in existing if re.search(r"(\d+)$", e)]
    next_num = max(nums, default=0) + 1
    bug_id = f"{prefix}-{next_num:03d}"

    bug = {
        "id": bug_id,
        "title": body.title,
        "status": "open",
        "severity": body.severity,
        "created": datetime.now(timezone.utc).isoformat(),
        "body": body.body,
    }

    filepath = bugs_dir / f"{bug_id}.md"
    filepath.write_text(_serialize_bug(bug), encoding="utf-8")

    bug["project"] = project_path
    return bug


@router.patch("/{session_id}/{bug_id}")
async def update_bug(request: Request, session_id: str, bug_id: str, body: BugUpdate):
    """Update an existing bug."""
    ws_base = _workspace_base(request)
    session_dir = ws_base / session_id
    if not session_dir.exists():
        raise HTTPException(status_code=404, detail="Session not found")

    # Find the bug file
    for bugs_dir in session_dir.rglob(".enclave-bugs"):
        target = bugs_dir / f"{bug_id}.md"
        if target.exists():
            bug = _parse_bug_file(target)
            if not bug:
                raise HTTPException(status_code=500, detail="Failed to parse bug file")

            # Apply updates
            if body.title is not None:
                bug["title"] = body.title
            if body.status is not None:
                bug["status"] = body.status
            if body.severity is not None:
                bug["severity"] = body.severity
            if body.body is not None:
                bug["body"] = body.body

            target.write_text(_serialize_bug(bug), encoding="utf-8")
            bug["project"] = str(bugs_dir.parent.relative_to(session_dir))
            return bug

    raise HTTPException(status_code=404, detail=f"Bug {bug_id} not found")


@router.delete("/{session_id}/{bug_id}")
async def delete_bug(request: Request, session_id: str, bug_id: str):
    """Delete a bug."""
    ws_base = _workspace_base(request)
    session_dir = ws_base / session_id
    if not session_dir.exists():
        raise HTTPException(status_code=404, detail="Session not found")

    for bugs_dir in session_dir.rglob(".enclave-bugs"):
        target = bugs_dir / f"{bug_id}.md"
        if target.exists():
            target.unlink()
            # Also remove attachments
            att_dir = bugs_dir / "attachments" / bug_id
            if att_dir.exists():
                shutil.rmtree(att_dir)
            return {"deleted": True, "bug_id": bug_id}

    raise HTTPException(status_code=404, detail=f"Bug {bug_id} not found")


# ─── Attachments ────────────────────────────────────────────────────────────


@router.get("/{session_id}/{bug_id}/attachments")
async def list_attachments(request: Request, session_id: str, bug_id: str):
    """List attachments for a bug."""
    ws_base = _workspace_base(request)
    session_dir = ws_base / session_id

    for bugs_dir in session_dir.rglob(".enclave-bugs"):
        att_dir = bugs_dir / "attachments" / bug_id
        if att_dir.exists():
            files = []
            for f in sorted(att_dir.iterdir()):
                if f.is_file():
                    files.append({
                        "name": f.name,
                        "size": f.stat().st_size,
                        "modified": datetime.fromtimestamp(
                            f.stat().st_mtime, tz=timezone.utc
                        ).isoformat(),
                    })
            return files

    return []


@router.post("/{session_id}/{bug_id}/attachments")
async def upload_attachment(
    request: Request, session_id: str, bug_id: str, file: UploadFile = File(...)
):
    """Upload an attachment to a bug."""
    ws_base = _workspace_base(request)
    session_dir = ws_base / session_id

    # Find the bugs dir containing this bug
    for bugs_dir in session_dir.rglob(".enclave-bugs"):
        target = bugs_dir / f"{bug_id}.md"
        if target.exists():
            att_dir = bugs_dir / "attachments" / bug_id
            att_dir.mkdir(parents=True, exist_ok=True)

            # Sanitize filename
            safe_name = "".join(
                c for c in (file.filename or "attachment")
                if c.isalnum() or c in "-_. "
            ).strip() or "attachment"

            dest = att_dir / safe_name
            async with aiofiles.open(dest, "wb") as f:
                content = await file.read()
                await f.write(content)

            return {
                "name": safe_name,
                "size": dest.stat().st_size,
                "bug_id": bug_id,
            }

    raise HTTPException(status_code=404, detail=f"Bug {bug_id} not found")
