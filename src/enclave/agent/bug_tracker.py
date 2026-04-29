"""Per-project bug tracking for agent sessions.

Stores bugs as markdown files with YAML-ish frontmatter under
``<workspace>/.enclave-bugs/<PREFIX>-NNN.md``. The workspace is the
source of truth (deterministic ID assignment, fast listing). When
Mimir is enabled, callers also fire-and-forget a Mimir record so the
bug becomes visible across sessions.

File layout::

    ---
    id: MEM-001
    status: open
    severity: medium
    created: 2026-04-29T03:05:57Z
    updated: 2026-04-29T03:05:57Z
    title: Old recall tool wins over mimir_recall
    ---

    ## Description

    ...

    ## Repro

    ...

    ## History

    - 2026-04-29 03:05Z [opened]: initial report
    - 2026-04-29 03:11Z [in_progress] Started rebuild

The tool tier is small and intentionally simple: open / update / list
/ get. Closing is just an update with status=resolved or wontfix.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

VALID_STATUS = ("open", "in_progress", "blocked", "resolved", "wontfix")
VALID_SEVERITY = ("low", "medium", "high", "critical")

_ID_RE = re.compile(r"^([A-Z]{2,8})-(\d{3,5})$")
_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


@dataclass
class Bug:
    id: str
    status: str
    severity: str
    title: str
    created: str
    updated: str
    body: str  # everything after frontmatter


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_short() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%MZ")


def compute_prefix(session_name: str | None) -> str:
    """Derive a bug-ID prefix from the session/project name.

    Takes the first 3 alphabetic characters of the name, uppercased.
    Falls back to ``BUG`` if no usable letters.
    """
    name = (session_name or "").strip()
    letters = [c for c in name if c.isalpha()]
    if len(letters) >= 3:
        return "".join(letters[:3]).upper()
    if letters:
        return ("".join(letters) + "BUG")[:3].upper()
    return "BUG"


def bug_dir(workspace: str | os.PathLike) -> Path:
    return Path(workspace) / ".enclave-bugs"


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    fm: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip()
    return fm, text[m.end():]


def _format_frontmatter(fm: dict[str, str]) -> str:
    keys = ("id", "status", "severity", "created", "updated", "title")
    lines = ["---"]
    for k in keys:
        if k in fm:
            lines.append(f"{k}: {fm[k]}")
    lines.append("---\n")
    return "\n".join(lines)


def load_bug(workspace: str | os.PathLike, bug_id: str) -> Bug | None:
    p = bug_dir(workspace) / f"{bug_id}.md"
    if not p.is_file():
        return None
    text = p.read_text()
    fm, body = _parse_frontmatter(text)
    if "id" not in fm:
        return None
    return Bug(
        id=fm.get("id", bug_id),
        status=fm.get("status", "open"),
        severity=fm.get("severity", "medium"),
        title=fm.get("title", ""),
        created=fm.get("created", ""),
        updated=fm.get("updated", ""),
        body=body,
    )


def list_bugs(
    workspace: str | os.PathLike,
    status_filter: str | None = None,
) -> list[Bug]:
    d = bug_dir(workspace)
    if not d.is_dir():
        return []
    bugs: list[Bug] = []
    for p in sorted(d.glob("*.md")):
        if not _ID_RE.match(p.stem):
            continue
        b = load_bug(workspace, p.stem)
        if not b:
            continue
        if status_filter and b.status != status_filter:
            continue
        bugs.append(b)
    return bugs


def next_id(workspace: str | os.PathLike, prefix: str) -> str:
    d = bug_dir(workspace)
    d.mkdir(parents=True, exist_ok=True)
    highest = 0
    for p in d.glob(f"{prefix}-*.md"):
        m = _ID_RE.match(p.stem)
        if m and m.group(1) == prefix:
            highest = max(highest, int(m.group(2)))
    return f"{prefix}-{highest + 1:03d}"


def open_bug(
    workspace: str | os.PathLike,
    *,
    prefix: str,
    title: str,
    description: str,
    repro: str = "",
    severity: str = "medium",
) -> Bug:
    if severity not in VALID_SEVERITY:
        severity = "medium"
    title = title.strip().replace("\n", " ")[:200] or "Untitled bug"
    bug_id = next_id(workspace, prefix)
    now = _now_iso()
    fm = {
        "id": bug_id,
        "status": "open",
        "severity": severity,
        "created": now,
        "updated": now,
        "title": title,
    }
    sections = [
        "## Description\n",
        description.strip() or "_(no description)_",
        "",
        "## Repro\n",
        repro.strip() or "_(none provided)_",
        "",
        "## History\n",
        f"- {_now_short()} [opened] severity={severity}",
        "",
    ]
    text = _format_frontmatter(fm) + "\n".join(sections)
    p = bug_dir(workspace) / f"{bug_id}.md"
    p.write_text(text)
    return load_bug(workspace, bug_id)  # type: ignore[return-value]


def update_bug(
    workspace: str | os.PathLike,
    bug_id: str,
    *,
    status: str | None = None,
    severity: str | None = None,
    note: str = "",
) -> Bug | None:
    p = bug_dir(workspace) / f"{bug_id}.md"
    if not p.is_file():
        return None
    text = p.read_text()
    fm, body = _parse_frontmatter(text)
    if status and status in VALID_STATUS:
        fm["status"] = status
    if severity and severity in VALID_SEVERITY:
        fm["severity"] = severity
    fm["updated"] = _now_iso()

    history_marker = "## History"
    note_clean = note.strip().replace("\n", " ")
    parts: list[str] = [f"- {_now_short()}"]
    if status and status in VALID_STATUS:
        parts.append(f"[{status}]")
    if severity and severity in VALID_SEVERITY:
        parts.append(f"severity={severity}")
    if note_clean:
        parts.append(note_clean)
    line = " ".join(parts)

    if history_marker in body:
        body = body.rstrip() + f"\n{line}\n"
    else:
        body = body.rstrip() + f"\n\n## History\n\n{line}\n"

    p.write_text(_format_frontmatter(fm) + body)
    return load_bug(workspace, bug_id)


def render_table(bugs: list[Bug]) -> str:
    if not bugs:
        return "_(no bugs)_"
    lines = [
        "| ID | Status | Severity | Title |",
        "|---|---|---|---|",
    ]
    for b in bugs:
        title = b.title.replace("|", "\\|")[:80]
        lines.append(f"| {b.id} | {b.status} | {b.severity} | {title} |")
    return "\n".join(lines)
