"""Data-driven definition of the ``consult_panel`` expert panel.

The panel is a set of "panelists", each an LLM sub-agent that critiques a
problem through a distinct lens. Historically these were hardcoded in the
agent. They are now data-driven so they can be edited (prompt, model,
enabled state) and extended with user-defined members via the Web UI.

Storage
-------
The editable panel lives in ``<data_dir>/panel.json`` on the orchestrator
host — i.e. *outside* the source tree. The built-in :data:`DEFAULT_PANEL`
only references publicly-known model ids, so adding private/preview model
ids to ``panel.json`` keeps them out of the repository.

The orchestrator seeds each session workspace with ``.enclave-panel.json``;
the in-container agent reads that file (falling back to :data:`DEFAULT_PANEL`
when absent), so the agent never needs host filesystem access.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PANEL_VERSION = 1

#: Filename the orchestrator writes into each session workspace and the agent
#: reads at ``consult_panel`` time.
WORKSPACE_PANEL_FILE = ".enclave-panel.json"


# Built-in panelists. Models are ordered preference lists — the first id that
# is actually available to the session wins. Only public model ids appear here;
# private/preview ids belong in the host-side panel.json, never in the repo.
DEFAULT_PANEL: list[dict[str, Any]] = [
    {
        "id": "architect",
        "name": "The Architect",
        "voice": (
            "'What does this look like in 2 years at 10x scale? Who "
            "maintains this?' You care about long-term stewardship, "
            "cohesion, extension points, and keeping the mental model "
            "clean."
        ),
        "focus": (
            "Coupling, hidden assumptions baked into code, decisions "
            "that are cheap now but expensive to reverse later, "
            "abstractions that will or won't hold up, interfaces that "
            "shape future work. Call out when a quick fix is actually "
            "a load-bearing decision in disguise."
        ),
        "models": [
            "claude-opus-4.8",
            "claude-opus-4.7-xhigh",
            "claude-opus-4.6",
            "claude-opus-4.5",
        ],
        "enabled": True,
    },
    {
        "id": "pragmatist",
        "name": "The Pragmatist",
        "voice": (
            "'What's the simplest thing that could work? Ship it, "
            "iterate later.' You distrust complexity, premature "
            "abstraction, and analysis paralysis. Think VERY hard and "
            "carefully before speaking — the engineer will rely on "
            "your judgment about what's truly necessary vs. what's "
            "gold-plating."
        ),
        "focus": (
            "YAGNI violations, over-engineering, scope creep, "
            "speculative generality. What's the smallest diff that "
            "actually solves the user's real problem today? What can "
            "be deleted, deferred, or faked? Call out when the "
            "engineer is solving a problem they don't actually have."
        ),
        "models": ["gpt-5.5", "gpt-5.4", "gpt-5.2"],
        "enabled": True,
    },
    {
        "id": "skeptic",
        "name": "The Skeptic",
        "voice": (
            "'How does this fail? What's the attacker's move? What "
            "if the input is null, malicious, or huge?' You assume "
            "the happy path is a lie and every assumption is wrong "
            "until proven otherwise."
        ),
        "focus": (
            "Edge cases, security holes, race conditions, silent "
            "failures, unvalidated inputs, data integrity, error "
            "paths, partial failures, concurrency bugs, trust "
            "boundaries. What inputs break this? What happens on a "
            "crash mid-operation? What does an adversary do?"
        ),
        "models": [
            "claude-opus-4.8",
            "claude-opus-4.6",
            "claude-opus-4.7",
            "claude-opus-4.5",
        ],
        "enabled": True,
    },
    {
        "id": "contrarian",
        "name": "The Contrarian",
        "voice": (
            "'What if the framing is wrong? What if we should do the "
            "literal opposite?' You question premises, flip "
            "assumptions, and look for the problem behind the "
            "problem."
        ),
        "focus": (
            "Unquestioned assumptions in how the problem is framed, "
            "false dichotomies, wrong-level solutions, cases where "
            "NOT doing the thing is the right answer. If everyone "
            "else is agreeing, dig for what they're all missing. "
            "Surface the option nobody proposed."
        ),
        "models": ["gpt-5.5", "claude-opus-4.7-xhigh", "claude-opus-4.6"],
        "enabled": True,
    },
]


def _slugify(value: str) -> str:
    out = "".join(c if c.isalnum() else "-" for c in value.lower()).strip("-")
    while "--" in out:
        out = out.replace("--", "-")
    return out or "member"


def _coerce_models(value: Any) -> list[str]:
    """Accept a list, or a comma/newline separated string, of model ids."""
    if isinstance(value, list):
        items = [str(v).strip() for v in value]
    elif isinstance(value, str):
        items = [p.strip() for p in value.replace("\n", ",").split(",")]
    else:
        items = []
    # De-dupe preserving order, drop empties.
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def normalize_member(raw: dict[str, Any], *, index: int = 0) -> dict[str, Any]:
    """Validate and normalize a single panelist definition."""
    name = str(raw.get("name") or "").strip() or f"Panelist {index + 1}"
    member_id = str(raw.get("id") or "").strip() or _slugify(name)
    return {
        "id": member_id,
        "name": name,
        "voice": str(raw.get("voice") or "").strip(),
        "focus": str(raw.get("focus") or "").strip(),
        "models": _coerce_models(raw.get("models")),
        "enabled": bool(raw.get("enabled", True)),
    }


def normalize_panel(data: Any) -> dict[str, Any]:
    """Coerce arbitrary input into a well-formed panel document."""
    if isinstance(data, dict):
        members = data.get("members", [])
    elif isinstance(data, list):
        members = data
    else:
        members = []
    if not isinstance(members, list):
        members = []

    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for i, raw in enumerate(members):
        if not isinstance(raw, dict):
            continue
        member = normalize_member(raw, index=i)
        # Ensure unique ids so the agent can address members deterministically.
        base_id = member["id"]
        uid = base_id
        n = 2
        while uid in seen_ids:
            uid = f"{base_id}-{n}"
            n += 1
        member["id"] = uid
        seen_ids.add(uid)
        normalized.append(member)

    return {"version": PANEL_VERSION, "members": normalized}


def default_panel() -> dict[str, Any]:
    """Return a fresh, normalized copy of the built-in panel."""
    return normalize_panel({"version": PANEL_VERSION, "members": DEFAULT_PANEL})


def panel_path(data_dir: str | Path) -> Path:
    """Path to the editable host-side panel definition."""
    return Path(data_dir) / "panel.json"


def load_panel(data_dir: str | Path) -> dict[str, Any]:
    """Load the host panel, seeding defaults to disk on first use."""
    path = panel_path(data_dir)
    if path.exists():
        try:
            return normalize_panel(json.loads(path.read_text()))
        except (OSError, ValueError):
            # Corrupt file — fall through to defaults rather than crash.
            pass
    panel = default_panel()
    try:
        save_panel(data_dir, panel)
    except OSError:
        pass
    return panel


def save_panel(data_dir: str | Path, data: Any) -> dict[str, Any]:
    """Normalize and persist the panel to the host data dir. Returns it."""
    panel = normalize_panel(data)
    path = panel_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(panel, indent=2))
    return panel


def load_workspace_panel(workspace: str | Path) -> dict[str, Any]:
    """Read the panel the orchestrator seeded into a session workspace.

    Falls back to the built-in default panel when the file is absent or
    unreadable, so ``consult_panel`` always has a usable roster.
    """
    path = Path(workspace) / WORKSPACE_PANEL_FILE
    if path.exists():
        try:
            panel = normalize_panel(json.loads(path.read_text()))
            if panel["members"]:
                return panel
        except (OSError, ValueError):
            pass
    return default_panel()


def write_workspace_panel(workspace: str | Path, data: Any) -> None:
    """Seed a session workspace with the current panel definition."""
    panel = normalize_panel(data)
    path = Path(workspace) / WORKSPACE_PANEL_FILE
    path.write_text(json.dumps(panel, indent=2))


def enabled_members(panel: dict[str, Any]) -> list[dict[str, Any]]:
    """Return only the panelists that are enabled and have a usable prompt."""
    return [
        m for m in panel.get("members", [])
        if m.get("enabled", True) and (m.get("voice") or m.get("focus"))
    ]


def build_panelist_prompt(member: dict[str, Any], problem: str) -> str:
    """Render the full instruction prompt for one panelist."""
    role = member.get("name") or member.get("id") or "Panelist"
    voice = member.get("voice") or ""
    focus = member.get("focus") or ""
    return (
        f"You are **{role}**, one member of an expert panel "
        "consulted by a fellow engineer who is stuck on a technical "
        "problem. The other panelists have different perspectives — "
        "your job is to bring YOUR distinct lens, not to produce a "
        "balanced take.\n\n"
        f"**Your voice:** {voice}\n\n"
        f"**What you look for:** {focus}\n\n"
        "**DO NOT do your own background research.** The calling "
        "engineer has already done the investigation and attached "
        "their findings in the problem description. Reason from "
        "the evidence they provided. Only fire off tool calls to "
        "research something if you have a specific idea that isn't "
        "covered by their attached material AND that idea is "
        "central to your recommendation — never for general "
        "background. If you want more evidence, name what's "
        "missing in your 'sharp question' instead of hunting for "
        "it yourself.\n\n"
        "Stay in character. Be direct, specific, and concrete. "
        "Do NOT hedge with 'it depends' — pick a position and defend "
        "it. Your perspective will be synthesized with the others, "
        "so redundancy with a balanced middle-ground is wasted effort.\n\n"
        "Structure your response as:\n"
        "1. **Your take** (2-4 sentences: the core point from your lens)\n"
        "2. **What the engineer is likely missing** (concrete risks or "
        "opportunities through your lens)\n"
        "3. **Concrete recommendation** (what you would do, and why)\n"
        "4. **A sharp question** (one question that if answered would "
        "materially change the approach)\n\n"
        "--- Problem Description ---\n"
        f"{problem}"
    )
