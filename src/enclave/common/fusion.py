"""Data-driven definition of the Fusion compound-model presets.

Fusion is a *transparent* compound model: a prompt is fanned out to a panel of
**diverse models** in parallel, a **judge** model extracts the structure of
their responses (consensus, contradictions, partial coverage, unique insights,
blind spots), and a **synthesizer** model writes the final answer grounded in
that analysis.

This is the model-diversity complement to ``consult_panel`` (which uses
*archetype* diversity and makes the calling agent synthesize). consult_panel
stays as-is; Fusion is the transparent, self-synthesizing version exposed as a
set of named presets ("fusion models").

Storage mirrors ``panel.py``: the editable presets live in
``<data_dir>/fusion.json`` on the orchestrator host (outside the source tree, so
private/preview model ids stay out of the repo). The orchestrator seeds each
session workspace with ``.enclave-fusion.json``; the in-container agent reads
that (falling back to :data:`DEFAULT_FUSION`).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FUSION_VERSION = 1

#: Filename the orchestrator writes into each session workspace and the agent
#: reads at fusion time.
WORKSPACE_FUSION_FILE = ".enclave-fusion.json"


# Built-in fusion presets. Each preset's ``participants``/``judge``/
# ``synthesizer`` are ordered preference lists — the first id actually available
# to the session wins. Only public model ids appear here; private/preview ids
# belong in the host-side fusion.json, never in the repo.
DEFAULT_FUSION: list[dict[str, Any]] = [
    {
        "id": "frontier",
        "name": "Frontier",
        "description": "Beyond-frontier: top models at max reasoning, judged and synthesized. Used for complexity 4-5.",
        "participants": [
            ["claude-opus-4.8-max", "claude-opus-4.8-xhigh", "claude-opus-4.8", "claude-opus-4.6"],
            ["gpt-5.5-xhigh", "gpt-5.5", "gpt-5.4"],
            ["claude-opus-4.7-xhigh", "claude-opus-4.7", "claude-sonnet-4.6"],
        ],
        "judge": ["claude-opus-4.8-max", "claude-opus-4.8-xhigh", "claude-opus-4.8", "gpt-5.5"],
        "synthesizer": ["claude-opus-4.8-max", "claude-opus-4.8-xhigh", "claude-opus-4.8", "gpt-5.5"],
        "enabled": True,
    },
    {
        "id": "balanced",
        "name": "Balanced",
        "description": "A mixed panel balancing quality and cost.",
        "participants": [
            ["claude-opus-4.6", "claude-opus-4.5"],
            ["gpt-5.4", "gpt-5.2"],
            ["claude-sonnet-4.6", "claude-sonnet-4.5"],
        ],
        "judge": ["claude-opus-4.6", "gpt-5.4"],
        "synthesizer": ["claude-opus-4.6", "gpt-5.4"],
        "enabled": True,
    },
    {
        "id": "budget",
        "name": "Budget",
        "description": "Cheap panel that punches above its weight via synthesis.",
        "participants": [
            ["claude-sonnet-4.6", "claude-sonnet-4.5"],
            ["gpt-5.4-mini", "gpt-5-mini"],
            ["gpt-4.1"],
        ],
        "judge": ["claude-sonnet-4.6", "gpt-5.4-mini"],
        "synthesizer": ["claude-sonnet-4.6", "gpt-5.4-mini"],
        "enabled": True,
    },
]


# ─── Normalization / persistence (mirrors panel.py) ─────────────────────────


def _slugify(value: str) -> str:
    out = "".join(c if c.isalnum() else "-" for c in value.lower()).strip("-")
    while "--" in out:
        out = out.replace("--", "-")
    return out or "preset"


def _coerce_models(value: Any) -> list[str]:
    """Accept a list, or a comma/newline separated string, of model ids."""
    if isinstance(value, list):
        items = [str(v).strip() for v in value]
    elif isinstance(value, str):
        items = [p.strip() for p in value.replace("\n", ",").split(",")]
    else:
        items = []
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _coerce_participants(value: Any) -> list[list[str]]:
    """Normalize the participants field into a list of preference lists.

    Each participant is one panel "seat" with an ordered model-preference list
    (first available wins). Accepts a list of lists, a list of strings (each
    becomes a single-model seat), or a newline/semicolon-separated string.
    """
    seats: list[list[str]] = []
    if isinstance(value, str):
        rows = [r for r in value.replace(";", "\n").split("\n") if r.strip()]
        for row in rows:
            models = _coerce_models(row)
            if models:
                seats.append(models)
        return seats
    if isinstance(value, list):
        for entry in value:
            if isinstance(entry, list):
                models = _coerce_models(entry)
            else:
                models = _coerce_models(str(entry))
            if models:
                seats.append(models)
    return seats


def normalize_preset(raw: dict[str, Any], *, index: int = 0) -> dict[str, Any]:
    """Validate and normalize a single fusion preset."""
    name = str(raw.get("name") or "").strip() or f"Fusion {index + 1}"
    preset_id = str(raw.get("id") or "").strip() or _slugify(name)
    return {
        "id": preset_id,
        "name": name,
        "description": str(raw.get("description") or "").strip(),
        "participants": _coerce_participants(raw.get("participants")),
        "judge": _coerce_models(raw.get("judge")),
        "synthesizer": _coerce_models(raw.get("synthesizer")),
        "enabled": bool(raw.get("enabled", True)),
    }


def normalize_fusion(data: Any) -> dict[str, Any]:
    """Coerce arbitrary input into a well-formed fusion document."""
    if isinstance(data, dict):
        presets = data.get("presets", [])
        base_model = data.get("base_model", "")
        auto_threshold = data.get("auto_threshold", 4)
    elif isinstance(data, list):
        presets, base_model, auto_threshold = data, "", 4
    else:
        presets, base_model, auto_threshold = [], "", 4
    if not isinstance(presets, list):
        presets = []

    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for i, raw in enumerate(presets):
        if not isinstance(raw, dict):
            continue
        preset = normalize_preset(raw, index=i)
        base_id = preset["id"]
        uid = base_id
        n = 2
        while uid in seen_ids:
            uid = f"{base_id}-{n}"
            n += 1
        preset["id"] = uid
        seen_ids.add(uid)
        normalized.append(preset)

    try:
        threshold = max(1, min(5, int(auto_threshold)))
    except (TypeError, ValueError):
        threshold = 4

    return {
        "version": FUSION_VERSION,
        "presets": normalized,
        # The cheap base model for Auto Fusion (escalates to a preset past the
        # complexity threshold). Empty = use the session's configured model.
        "base_model": str(base_model or "").strip(),
        "auto_threshold": threshold,
    }


def default_fusion() -> dict[str, Any]:
    """Return a fresh, normalized copy of the built-in fusion presets."""
    return normalize_fusion({
        "version": FUSION_VERSION,
        "presets": DEFAULT_FUSION,
        "base_model": "claude-sonnet-4.6",
        "auto_threshold": 4,
    })


def fusion_path(data_dir: str | Path) -> Path:
    """Path to the editable host-side fusion definition."""
    return Path(data_dir) / "fusion.json"


def load_fusion(data_dir: str | Path) -> dict[str, Any]:
    """Load the host fusion presets, seeding defaults to disk on first use."""
    path = fusion_path(data_dir)
    if path.exists():
        try:
            return normalize_fusion(json.loads(path.read_text()))
        except (OSError, ValueError):
            pass
    fusion = default_fusion()
    try:
        save_fusion(data_dir, fusion)
    except OSError:
        pass
    return fusion


def save_fusion(data_dir: str | Path, data: Any) -> dict[str, Any]:
    """Normalize and persist the fusion presets. Returns the normalized doc."""
    fusion = normalize_fusion(data)
    path = fusion_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fusion, indent=2))
    return fusion


def load_workspace_fusion(workspace: str | Path) -> dict[str, Any]:
    """Read the fusion presets the orchestrator seeded into a session workspace."""
    path = Path(workspace) / WORKSPACE_FUSION_FILE
    if path.exists():
        try:
            fusion = normalize_fusion(json.loads(path.read_text()))
            if fusion["presets"]:
                return fusion
        except (OSError, ValueError):
            pass
    return default_fusion()


def write_workspace_fusion(workspace: str | Path, data: Any) -> None:
    """Seed a session workspace with the current fusion definition."""
    fusion = normalize_fusion(data)
    path = Path(workspace) / WORKSPACE_FUSION_FILE
    path.write_text(json.dumps(fusion, indent=2))


def enabled_presets(fusion: dict[str, Any]) -> list[dict[str, Any]]:
    """Return only presets that are enabled and have at least one participant."""
    return [
        p for p in fusion.get("presets", [])
        if p.get("enabled", True) and p.get("participants")
    ]


# ─── Per-session fusion mode (pickable as a "model") ────────────────────────

#: Workspace file recording the session's selected fusion mode, set when the
#: user picks a fusion/auto-fusion entry in the model picker. Read per-turn by
#: the agent so the choice takes effect at runtime (no restart).
WORKSPACE_FUSION_MODE_FILE = ".enclave-fusion-mode"

#: Pseudo-model id for Auto Fusion (self-grade + escalate) in the picker.
AUTO_FUSION_MODEL_ID = "auto-fusion"
#: Prefix for "always use this preset" pseudo-models, e.g. "fusion:frontier".
FUSION_MODEL_PREFIX = "fusion:"


def read_fusion_mode(workspace: str | Path) -> str:
    """Return the session's fusion mode id, or "" if none.

    Values: "auto-fusion", "fusion:<presetid>", or "".
    """
    path = Path(workspace) / WORKSPACE_FUSION_MODE_FILE
    if path.exists():
        try:
            data = json.loads(path.read_text())
            return str(data.get("mode", "") or "")
        except (OSError, ValueError):
            return ""
    return ""


def write_fusion_mode(workspace: str | Path, mode: str) -> None:
    """Persist the session's fusion mode (empty clears it)."""
    path = Path(workspace) / WORKSPACE_FUSION_MODE_FILE
    if not mode:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return
    path.write_text(json.dumps({"mode": mode}))


def is_fusion_model(model_id: str) -> bool:
    """Whether a model-picker id refers to a fusion mode (not a real model)."""
    return model_id == AUTO_FUSION_MODEL_ID or model_id.startswith(FUSION_MODEL_PREFIX)


def fusion_model_ids(fusion: dict[str, Any]) -> list[str]:
    """Pseudo-model ids to surface in the picker: auto-fusion + each preset."""
    ids = [AUTO_FUSION_MODEL_ID]
    for p in enabled_presets(fusion):
        ids.append(f"{FUSION_MODEL_PREFIX}{p['id']}")
    return ids


def get_preset(fusion: dict[str, Any], preset_id: str) -> dict[str, Any] | None:
    """Find a preset by id (or name slug). Returns None if not found/enabled."""
    wanted = _slugify(preset_id)
    for p in enabled_presets(fusion):
        if p["id"] == preset_id or _slugify(p["name"]) == wanted or p["id"] == wanted:
            return p
    return None


# ─── Prompt builders ────────────────────────────────────────────────────────


def build_participant_prompt(problem: str) -> str:
    """Neutral prompt for a fusion participant.

    Unlike consult_panel (archetype personas), fusion participants all get the
    SAME neutral prompt — the diversity comes from the different models, not
    different instructions. We want each model's honest best answer.
    """
    return (
        "You are one member of a panel of AI models answering the same "
        "question independently. Your response will be combined with the "
        "others by a synthesizer, so give YOUR genuine best answer — be "
        "specific, correct, and complete. State your reasoning and call out "
        "any assumptions, uncertainties, or places you might be wrong. "
        "Don't hedge needlessly; commit to concrete answers where you can.\n\n"
        "--- Question ---\n"
        f"{problem}"
    )


def build_judge_prompt(problem: str, responses: list[tuple[str, str]]) -> str:
    """Prompt for the judge model: extract structure from the panel responses."""
    blocks = []
    for i, (label, text) in enumerate(responses, 1):
        blocks.append(f"### Response {i} (from {label})\n{text}")
    joined = "\n\n".join(blocks)
    return (
        "You are the JUDGE in a compound-model system. Several AI models "
        "answered the same question independently. Read every response and "
        "extract the structure of their collective reasoning. Do NOT write a "
        "final answer — your job is analysis that a synthesizer will ground "
        "its answer in.\n\n"
        "Produce a concise structured analysis with these sections:\n"
        "1. **Consensus** — points (most/all) agree on; treat as high-confidence.\n"
        "2. **Contradictions** — where they directly disagree, and which side "
        "is better supported (say why).\n"
        "3. **Partial coverage** — important points only some raised.\n"
        "4. **Unique insights** — a single model's non-obvious correct point "
        "worth keeping.\n"
        "5. **Blind spots** — what they ALL missed or got wrong (use your own "
        "knowledge here).\n"
        "6. **Confidence** — overall, how settled is the answer (low/med/high)?\n\n"
        f"--- Original question ---\n{problem}\n\n"
        f"--- Panel responses ---\n{joined}"
    )


def build_synthesizer_prompt(
    problem: str, judge_analysis: str, responses: list[tuple[str, str]],
) -> str:
    """Prompt for the synthesizer: write the final answer from the judge analysis."""
    blocks = []
    for i, (label, text) in enumerate(responses, 1):
        blocks.append(f"### Response {i} (from {label})\n{text}")
    joined = "\n\n".join(blocks)
    return (
        "You are the SYNTHESIZER in a compound-model system. A judge has "
        "analyzed several models' independent answers to a question. Write the "
        "single best final answer, grounded in the judge's analysis:\n"
        "- Lead with the consensus (high-confidence) content.\n"
        "- Resolve contradictions in favour of the better-supported side; if "
        "genuinely unsettled, say so briefly and give the safest course.\n"
        "- Fold in the unique insights and cover the blind spots the judge "
        "flagged.\n"
        "- Be direct and complete; answer the question as if it were asked of "
        "you. Do NOT mention 'the panel', 'the judge', or this process — just "
        "give the answer.\n\n"
        f"--- Original question ---\n{problem}\n\n"
        f"--- Judge's analysis ---\n{judge_analysis}\n\n"
        f"--- Raw panel responses (for detail) ---\n{joined}"
    )


def build_complexity_prompt(task: str) -> str:
    """Prompt for the complexity grader (Auto Fusion).

    Returns a small JSON object the caller parses: a 1-5 complexity score, a
    recommended tier, and a one-line rationale.
    """
    return (
        "You are a fast task-complexity grader for an AI coding agent. Given "
        "the task/turn below, rate on a 1-5 scale how much it would benefit "
        "from a panel of models (Fusion) versus a single capable model. "
        "Consider: ambiguity, architectural/design stakes, breadth of "
        "knowledge required, risk of subtle error, and whether multiple valid "
        "approaches exist.\n\n"
        "Scale:\n"
        "1 = trivial/mechanical (rename, format, obvious one-liner)\n"
        "2 = simple, well-specified\n"
        "3 = moderate; some judgement but a single model handles it\n"
        "4 = complex; design stakes or subtle risk — a panel helps\n"
        "5 = very complex; open-ended planning, tricky algorithm/architecture, "
        "high-risk change — a panel clearly helps\n\n"
        "Respond with ONLY a compact JSON object, no prose:\n"
        '{"score": <1-5 int>, "tier": "base"|"fusion", '
        '"reason": "<one short sentence>"}'
        "\n\n--- Task ---\n"
        f"{task}"
    )


def parse_complexity(raw: str, *, threshold: int = 4) -> dict[str, Any]:
    """Parse a grader response into {score:1-5, tier, reason}.

    Tolerant of code fences / surrounding prose: extracts the first JSON object.
    Falls back to a mid score if unparseable. `tier` is derived from the score
    vs threshold when the model didn't supply a clean one.
    """
    score = 3
    reason = ""
    tier = ""
    text = (raw or "").strip()
    # Find the first {...} block.
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            obj = json.loads(text[start:end + 1])
            score = int(obj.get("score", 3))
            reason = str(obj.get("reason", "")).strip()
            tier = str(obj.get("tier", "")).strip().lower()
        except (ValueError, TypeError):
            pass
    score = max(1, min(5, score))
    if tier not in ("base", "fusion"):
        tier = "fusion" if score >= threshold else "base"
    return {"score": score, "tier": tier, "reason": reason}
