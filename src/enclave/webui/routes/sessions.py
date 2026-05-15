"""Session management API routes.

Provides endpoints to list, start, stop, restart sessions,
inspect and clear state, and manage named snapshots.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import zipfile
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from enclave.webui.auth import validate_token

router = APIRouter()
# Separate router for endpoints that accept ?token= query-param auth
# (browser <img src> / <a href> can't send Authorization headers).
public_router = APIRouter()


# ─── Helpers ────────────────────────────────────────────────────────────────


def _get_config(request: Request):
    return request.app.state.config


def _workspace_base(request: Request) -> Path:
    return Path(_get_config(request).container.workspace_base)


def _session_base(request: Request) -> Path:
    return Path(_get_config(request).container.session_base)


def _socket_dir(request: Request) -> Path:
    return Path(_get_config(request).container.socket_dir)


def _snapshots_dir() -> Path:
    d = Path.home() / ".local" / "share" / "enclave" / "snapshots"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _discover_sessions(request: Request) -> list[dict[str, Any]]:
    """Discover sessions from workspace directories."""
    ws_base = _workspace_base(request)
    sessions = []
    if not ws_base.exists():
        return sessions

    # Load orchestrator sessions.json for extra metadata (ACP fields etc.)
    orch_sessions: dict[str, dict] = {}
    orch_file = _session_base(request) / "sessions.json"
    if orch_file.exists():
        try:
            for s in json.loads(orch_file.read_text()):
                orch_sessions[s["id"]] = s
        except Exception:
            pass

    for entry in sorted(ws_base.iterdir()):
        if not entry.is_dir():
            continue
        session_id = entry.name
        # Check if agent socket exists (indicates running)
        socket_path = _socket_dir(request) / f"{session_id}.sock"
        is_running = socket_path.exists()

        # Try to get session name and archived status from config
        name = session_id
        archived = False
        config_file = entry / ".enclave-session.json"
        if config_file.exists():
            try:
                data = json.loads(config_file.read_text())
                name = data.get("name", session_id)
                archived = data.get("archived", False)
            except Exception:
                pass

        orch = orch_sessions.get(session_id, {})
        if orch.get("name"):
            name = orch["name"]

        info: dict[str, Any] = {
            "id": session_id,
            "name": name,
            "status": "running" if is_running else "stopped",
            "archived": archived,
            "workspace": str(entry),
        }

        # Include ACP remote info if present
        if orch.get("acp_host"):
            info["acp_host"] = orch["acp_host"]
            info["acp_port"] = orch.get("acp_port", 0)
            info["acp_remote"] = True

        sessions.append(info)

    return sessions


def _get_session_workspace(request: Request, session_id: str) -> Path:
    """Get workspace path for a session, raise 404 if not found."""
    ws = _workspace_base(request) / session_id
    if not ws.exists():
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
    return ws


def _copilot_state_dir(request: Request, session_id: str) -> Path:
    """Get the .copilot-state directory for a session."""
    ws = _get_session_workspace(request, session_id)
    state_dir = ws / ".copilot-state"
    return state_dir


# ─── Models ─────────────────────────────────────────────────────────────────


class SnapshotCreate(BaseModel):
    name: str


# ─── Session List / Status ──────────────────────────────────────────────────


@router.get("")
async def list_sessions(request: Request):
    """List all known sessions with their status."""
    return _discover_sessions(request)


@router.get("/{session_id}")
async def get_session(request: Request, session_id: str):
    """Get details for a specific session."""
    sessions = _discover_sessions(request)
    for s in sessions:
        if s["id"] == session_id:
            return s
    raise HTTPException(status_code=404, detail=f"Session {session_id} not found")


# ─── Session Control ────────────────────────────────────────────────────────


@router.post("/{session_id}/stop")
async def stop_session(request: Request, session_id: str):
    """Stop a running session by sending a stop command via IPC."""
    # For now, use podman stop directly
    proc = await asyncio.create_subprocess_exec(
        "podman", "stop", "-t", "10", session_id,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        err = stderr.decode().strip()
        if "no such container" in err.lower() or "not found" in err.lower():
            raise HTTPException(status_code=404, detail=f"Container {session_id} not found or not running")
        raise HTTPException(status_code=500, detail=f"Failed to stop: {err}")
    return {"status": "stopped", "session_id": session_id}


@router.post("/{session_id}/restart")
async def restart_session(request: Request, session_id: str):
    """Restart a session (stop + orchestrator will auto-restart)."""
    # Stop container — orchestrator auto-restores on next sync
    proc = await asyncio.create_subprocess_exec(
        "podman", "stop", "-t", "10", session_id,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()
    return {"status": "restarting", "session_id": session_id}


@router.post("/{session_id}/archive")
async def archive_session(request: Request, session_id: str):
    """Toggle the archived status of a session."""
    ws = _workspace_base(request) / session_id
    if not ws.exists():
        raise HTTPException(status_code=404, detail="Session not found")

    config_file = ws / ".enclave-session.json"
    data = {}
    if config_file.exists():
        try:
            data = json.loads(config_file.read_text())
        except Exception:
            pass

    data["archived"] = not data.get("archived", False)
    config_file.write_text(json.dumps(data, indent=2))
    return {"session_id": session_id, "archived": data["archived"]}


# ─── State Inspection ───────────────────────────────────────────────────────


@router.get("/{session_id}/state")
async def get_state_tree(request: Request, session_id: str):
    """Get the file tree of the .copilot-state directory."""
    state_dir = _copilot_state_dir(request, session_id)
    if not state_dir.exists():
        return {"files": [], "exists": False}

    files = []
    for root, dirs, filenames in os.walk(state_dir):
        # Skip __pycache__ and .git
        dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git", "node_modules")]
        for fname in sorted(filenames):
            fpath = Path(root) / fname
            rel = fpath.relative_to(state_dir)
            try:
                size = fpath.stat().st_size
            except OSError:
                size = 0
            files.append({
                "path": str(rel),
                "size": size,
                "modified": datetime.fromtimestamp(
                    fpath.stat().st_mtime, tz=timezone.utc
                ).isoformat() if fpath.exists() else None,
            })
    return {"files": files, "exists": True}


@router.get("/{session_id}/state/{file_path:path}")
async def get_state_file(request: Request, session_id: str, file_path: str):
    """Read the contents of a specific state file."""
    state_dir = _copilot_state_dir(request, session_id)
    target = state_dir / file_path

    # Security: prevent path traversal
    try:
        target.resolve().relative_to(state_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Path traversal not allowed")

    if not target.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {file_path}")
    if not target.is_file():
        raise HTTPException(status_code=400, detail="Not a file")

    # Read as text if possible, binary as base64 otherwise
    try:
        content = target.read_text(encoding="utf-8")
        return {"path": file_path, "content": content, "encoding": "utf-8"}
    except UnicodeDecodeError:
        import base64
        content = base64.b64encode(target.read_bytes()).decode("ascii")
        return {"path": file_path, "content": content, "encoding": "base64"}


@router.delete("/{session_id}/state")
async def clear_state(request: Request, session_id: str):
    """Clear the .copilot-state directory for a session."""
    state_dir = _copilot_state_dir(request, session_id)
    if not state_dir.exists():
        return {"cleared": False, "reason": "no state directory"}

    # Remove and recreate empty
    shutil.rmtree(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    return {"cleared": True, "session_id": session_id}


# ─── Snapshots ──────────────────────────────────────────────────────────────


def _session_snapshots_dir(session_id: str) -> Path:
    d = _snapshots_dir() / session_id
    d.mkdir(parents=True, exist_ok=True)
    return d


@router.get("/{session_id}/snapshots")
async def list_snapshots(session_id: str):
    """List all named snapshots for a session."""
    snap_dir = _session_snapshots_dir(session_id)
    snapshots = []
    for f in sorted(snap_dir.iterdir()):
        if f.suffix == ".zip":
            snapshots.append({
                "name": f.stem,
                "filename": f.name,
                "size": f.stat().st_size,
                "created": datetime.fromtimestamp(
                    f.stat().st_ctime, tz=timezone.utc
                ).isoformat(),
            })
    return snapshots


@router.post("/{session_id}/snapshots")
async def create_snapshot(request: Request, session_id: str, body: SnapshotCreate):
    """Create a named snapshot of the session's .copilot-state."""
    state_dir = _copilot_state_dir(request, session_id)
    if not state_dir.exists():
        raise HTTPException(status_code=404, detail="No state directory to snapshot")

    snap_dir = _session_snapshots_dir(session_id)
    # Sanitize name
    safe_name = "".join(c for c in body.name if c.isalnum() or c in "-_ ").strip()
    if not safe_name:
        raise HTTPException(status_code=400, detail="Invalid snapshot name")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = f"{safe_name}_{timestamp}.zip"
    zip_path = snap_dir / filename

    # Create zip of state dir
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(state_dir):
            dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git")]
            for fname in files:
                fpath = Path(root) / fname
                arcname = fpath.relative_to(state_dir)
                zf.write(fpath, arcname)

    return {
        "name": safe_name,
        "filename": filename,
        "size": zip_path.stat().st_size,
        "created": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/{session_id}/snapshots/{filename}/download")
async def download_snapshot(session_id: str, filename: str):
    """Download a snapshot as a zip file."""
    snap_dir = _session_snapshots_dir(session_id)
    zip_path = snap_dir / filename

    if not zip_path.exists() or not zip_path.suffix == ".zip":
        raise HTTPException(status_code=404, detail="Snapshot not found")

    # Security check
    try:
        zip_path.resolve().relative_to(snap_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Invalid path")

    def iter_file():
        with open(zip_path, "rb") as f:
            while chunk := f.read(65536):
                yield chunk

    return StreamingResponse(
        iter_file(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/{session_id}/snapshots/{filename}/restore")
async def restore_snapshot(request: Request, session_id: str, filename: str):
    """Restore a snapshot, replacing current .copilot-state."""
    snap_dir = _session_snapshots_dir(session_id)
    zip_path = snap_dir / filename

    if not zip_path.exists() or not zip_path.suffix == ".zip":
        raise HTTPException(status_code=404, detail="Snapshot not found")

    state_dir = _copilot_state_dir(request, session_id)

    # Clear existing state
    if state_dir.exists():
        shutil.rmtree(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)

    # Extract snapshot
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(state_dir)

    return {"restored": True, "from": filename, "session_id": session_id}


@router.delete("/{session_id}/snapshots/{filename}")
async def delete_snapshot(session_id: str, filename: str):
    """Delete a named snapshot."""
    snap_dir = _session_snapshots_dir(session_id)
    zip_path = snap_dir / filename

    if not zip_path.exists():
        raise HTTPException(status_code=404, detail="Snapshot not found")

    zip_path.unlink()
    return {"deleted": True, "filename": filename}


# ─── Logs ───────────────────────────────────────────────────────────────────


@router.get("/{session_id}/logs")
async def get_logs(request: Request, session_id: str, lines: int = 100, since: str | None = None):
    """Get recent log lines for a session from journalctl."""
    cmd = [
        "journalctl", "--user", "-u", "enclave",
        "--no-pager", "-n", str(min(lines, 5000)),
        "-o", "short-iso",
    ]
    if since:
        cmd.extend(["--since", since])

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
    all_lines = stdout.decode("utf-8", errors="replace").split("\n")

    # Filter to lines containing the session_id
    filtered = [ln for ln in all_lines if session_id in ln]
    return {"lines": filtered[-lines:], "session_id": session_id}


@router.websocket("/{session_id}/logs/stream")
async def stream_logs(websocket: WebSocket, session_id: str):
    """WebSocket endpoint for live-tailing session logs."""
    await websocket.accept()

    proc = await asyncio.create_subprocess_exec(
        "journalctl", "--user", "-u", "enclave",
        "--no-pager", "-f", "-o", "short-iso",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        while True:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=30.0)
            if not line:
                break
            decoded = line.decode("utf-8", errors="replace").rstrip()
            if session_id in decoded:
                await websocket.send_text(decoded)
    except (WebSocketDisconnect, asyncio.TimeoutError, asyncio.CancelledError):
        pass
    finally:
        proc.kill()
        await proc.wait()


# ─── System Prompt ──────────────────────────────────────────────────────────

PROMPT_FILENAME = ".enclave-session-prompt"


class PromptUpdate(BaseModel):
    content: str


@router.get("/{session_id}/prompt")
async def get_prompt(request: Request, session_id: str):
    """Read the session-specific system prompt."""
    ws = _workspace_base(request) / session_id
    if not ws.exists():
        raise HTTPException(status_code=404, detail="Session not found")

    prompt_path = ws / PROMPT_FILENAME
    content = ""
    if prompt_path.exists():
        content = prompt_path.read_text(encoding="utf-8")

    # Also return the base prompt files for reference
    prompts_dir = Path(__file__).parent.parent.parent / "agent" / "prompts"
    base_parts = {}
    for name in ("base.md", "guidelines.md", "dev.md", "host.md"):
        fp = prompts_dir / name
        if fp.exists():
            base_parts[name] = fp.read_text(encoding="utf-8")

    return {
        "session_prompt": content,
        "base_prompts": base_parts,
        "path": str(prompt_path),
    }


@router.put("/{session_id}/prompt")
async def update_prompt(request: Request, session_id: str, body: PromptUpdate):
    """Write or update the session-specific system prompt."""
    ws = _workspace_base(request) / session_id
    if not ws.exists():
        raise HTTPException(status_code=404, detail="Session not found")

    prompt_path = ws / PROMPT_FILENAME
    prompt_path.write_text(body.content, encoding="utf-8")
    return {"saved": True, "path": str(prompt_path)}


# ─── Artifacts ──────────────────────────────────────────────────────────────

ARTIFACTS_MANIFEST = ".enclave-artifacts.json"


def _artifacts_dir(request: Request, session_id: str) -> Path:
    ws = _workspace_base(request) / session_id / "artifacts"
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def _read_manifest(request: Request, session_id: str) -> list[dict]:
    manifest_path = _workspace_base(request) / session_id / ARTIFACTS_MANIFEST
    if not manifest_path.exists():
        return []
    try:
        return json.loads(manifest_path.read_text())
    except (json.JSONDecodeError, OSError):
        return []


def _write_manifest(request: Request, session_id: str, entries: list[dict]) -> None:
    manifest_path = _workspace_base(request) / session_id / ARTIFACTS_MANIFEST
    manifest_path.write_text(json.dumps(entries, indent=2))


class ArtifactRegister(BaseModel):
    title: str
    description: str = ""
    filename: str
    content_type: str = "text/markdown"


@router.get("/{session_id}/artifacts")
async def list_artifacts(request: Request, session_id: str):
    """List all registered artifacts, most recent first."""
    entries = _read_manifest(request, session_id)
    entries.sort(key=lambda e: e.get("created", ""), reverse=True)
    return entries


@router.post("/{session_id}/artifacts")
async def register_artifact(request: Request, session_id: str, body: ArtifactRegister):
    """Register a new artifact. The file must already exist in the artifacts/ dir."""
    art_dir = _artifacts_dir(request, session_id)
    target = art_dir / body.filename

    # Security: prevent traversal
    try:
        target.resolve().relative_to(art_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Path traversal not allowed")

    if not target.exists():
        raise HTTPException(status_code=404, detail=f"File not found in artifacts/: {body.filename}")

    entries = _read_manifest(request, session_id)
    now = datetime.now(timezone.utc).isoformat()

    existing = next((e for e in entries if e["filename"] == body.filename), None)
    if existing:
        # Version the old content before updating
        version = existing.get("version", 1)
        old_size = target.stat().st_size  # actual file size
        stem = Path(body.filename).stem
        ext = Path(body.filename).suffix
        versioned_name = f"{stem}.v{version}{ext}"
        versioned_path = art_dir / versioned_name
        if not versioned_path.exists():
            # Only version if we haven't already (agent tool may have done it)
            shutil.copy2(str(target), str(versioned_path))

        versions = existing.get("versions", [])
        if not versions:
            versions.append({
                "version": 1,
                "created": existing.get("created", now),
                "size": old_size if version == 1 else existing.get("size", 0),
            })
        # Add current version to history (avoid duplicate v1)
        if version > 1 or not versions:
            versions.append({
                "version": version,
                "created": existing.get("updated", now),
                "size": old_size,
            })

        existing.update({
            "title": body.title,
            "description": body.description,
            "content_type": body.content_type,
            "version": version + 1,
            "versions": versions,
            "updated": now,
            "size": target.stat().st_size,
        })
    else:
        entries.append({
            "title": body.title,
            "description": body.description,
            "filename": body.filename,
            "content_type": body.content_type,
            "size": target.stat().st_size,
            "version": 1,
            "versions": [],
            "created": now,
            "updated": now,
        })
    _write_manifest(request, session_id, entries)
    return {"registered": True, "filename": body.filename}


@router.get("/{session_id}/artifacts/{filename:path}/versions")
async def get_artifact_versions(request: Request, session_id: str, filename: str):
    """List version history for an artifact."""
    entries = _read_manifest(request, session_id)
    entry = next((e for e in entries if e["filename"] == filename), None)
    if not entry:
        raise HTTPException(status_code=404, detail="Artifact not found")

    versions = entry.get("versions", [])
    current_version = entry.get("version", 1)
    return {
        "filename": filename,
        "current_version": current_version,
        "versions": versions,
    }


@router.get("/{session_id}/artifacts/{filename:path}/diff")
async def get_artifact_diff(request: Request, session_id: str, filename: str, v1: int = 1, v2: int = 0):
    """Get a unified diff between two versions of an artifact.

    v2=0 means the current (latest) version.
    """
    import difflib

    art_dir = _artifacts_dir(request, session_id)
    entries = _read_manifest(request, session_id)
    entry = next((e for e in entries if e["filename"] == filename), None)
    if not entry:
        raise HTTPException(status_code=404, detail="Artifact not found")

    current_version = entry.get("version", 1)
    stem = Path(filename).stem
    ext = Path(filename).suffix

    # Resolve file paths for each version
    def _version_path(v: int) -> Path:
        if v == current_version or v == 0:
            return art_dir / filename
        return art_dir / f"{stem}.v{v}{ext}"

    path1 = _version_path(v1)
    path2 = _version_path(v2 if v2 else current_version)

    if not path1.exists():
        raise HTTPException(status_code=404, detail=f"Version {v1} not found")
    if not path2.exists():
        raise HTTPException(status_code=404, detail=f"Version {v2 or current_version} not found")

    try:
        text1 = path1.read_text(encoding="utf-8").splitlines(keepends=True)
        text2 = path2.read_text(encoding="utf-8").splitlines(keepends=True)
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="Cannot diff binary files")

    label1 = f"{filename} (v{v1})"
    label2 = f"{filename} (v{v2 or current_version})"
    diff = list(difflib.unified_diff(text1, text2, fromfile=label1, tofile=label2))
    return {
        "v1": v1,
        "v2": v2 or current_version,
        "diff": "".join(diff),
        "has_changes": len(diff) > 0,
    }


@public_router.get("/{session_id}/artifacts/{filename:path}")
async def get_artifact(
    request: Request,
    session_id: str,
    filename: str,
    token: str = Query(None),
):
    """Serve an artifact file.

    Auth via ``?token=`` query parameter so that ``<a href>`` and
    ``window.open()`` links work without JavaScript fetch.
    """
    if token:
        validate_token(token)
    else:
        # If no query token, require the normal Authorization header
        from enclave.webui.auth import get_current_user, oauth2_scheme
        from fastapi import Depends
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            validate_token(auth_header[7:])
        else:
            raise HTTPException(status_code=401, detail="Not authenticated")

    art_dir = _artifacts_dir(request, session_id)
    target = art_dir / filename

    try:
        target.resolve().relative_to(art_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Path traversal not allowed")

    if not target.exists():
        raise HTTPException(status_code=404, detail="Artifact not found")

    # For text files, return content as JSON
    if filename.endswith(('.md', '.txt', '.json', '.yaml', '.yml', '.csv', '.log')):
        try:
            content = target.read_text(encoding="utf-8")
            return {"filename": filename, "content": content, "encoding": "utf-8"}
        except UnicodeDecodeError:
            pass

    # For images and binary files, serve directly
    import mimetypes
    ct = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return FileResponse(target, media_type=ct)

