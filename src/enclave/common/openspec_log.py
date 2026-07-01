"""Shared OpenSpec review-log primitives (no web framework dependency).

Both the web UI route (``enclave.webui.routes.openspec``) and the agent tool
(``openspec_revision_log``) use these so there is exactly one implementation of
the append-only event log, atomic writes, content-addressed blob store, and file
snapshots. Keeping them here (in ``common``) avoids importing FastAPI into the
agent process.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

REVIEW_FILE = ".enclave-review.json"


def content_hash(content: str) -> str:
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def read_log(change_dir: Path) -> dict:
    """Load the append-only review log, tolerating absence/corruption."""
    path = change_dir / REVIEW_FILE
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "events": []}
    if not isinstance(doc.get("events"), list):
        doc["events"] = []
    return doc


def write_log_atomic(change_dir: Path, doc: dict) -> None:
    """Write the log via temp-file + rename so a concurrent read never sees a
    half-written file."""
    target = change_dir / REVIEW_FILE
    tmp = change_dir / f".{REVIEW_FILE}.{os.getpid()}.{int(time.time() * 1000)}.tmp"
    tmp.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    os.replace(str(tmp), str(target))


def snapshot_files(change_dir: Path, root: Path) -> tuple[dict[str, str], dict[str, str]]:
    """Snapshot a change's markdown files.

    Returns ``(snapshot, blobs)`` where ``snapshot`` maps each file path
    (relative to ``root``) to a content hash and ``blobs`` maps hash -> content.
    """
    snap: dict[str, str] = {}
    blobs: dict[str, str] = {}
    for p in sorted(change_dir.rglob("*.md")):
        try:
            content = p.read_text(encoding="utf-8")
        except OSError:
            continue
        rel = str(p.relative_to(root))
        h = content_hash(content)
        snap[rel] = h
        blobs[h] = content
    return snap, blobs


def append_event(change_dir: Path, event: dict, blobs: dict[str, str] | None = None) -> dict:
    """Append one event to the log (idempotent on event id) and return the doc.

    ``blobs`` (hash -> content) are merged into the doc's content-addressed blob
    store so snapshots referenced by the event can be reconstructed for diffing.
    Top-level badge fields are synced for ``review`` events.
    """
    doc = read_log(change_dir)
    if event.get("id") and any(e.get("id") == event["id"] for e in doc["events"]):
        return doc  # idempotent
    if blobs:
        doc.setdefault("blobs", {}).update(blobs)
    doc["events"].append(event)
    if event.get("type") == "review":
        doc["state"] = event.get("state")
        doc["by"] = event.get("by")
        doc["at"] = event.get("at")
    write_log_atomic(change_dir, doc)
    return doc
