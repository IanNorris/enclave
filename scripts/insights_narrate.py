"""LLM narrative layer for Enclave Insights.

Feeds the *narrowed signal* (computed metrics + mined corrections + highlight
moments) — never raw events — to the bundled copilot CLI to write the prose
report sections and, most importantly, distil the user's recurring corrections
into concrete base-prompt additions.

Kept separate from metric collection so the deterministic layer stays LLM-free
and fast; this is opt-in via --narrate.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections import Counter
from pathlib import Path


def _copilot_bin() -> str:
    for cand in (
        Path.home() / ".npm-global/bin/copilot",
        Path("/usr/local/bin/copilot"),
    ):
        if cand.exists():
            return str(cand)
    return "copilot"


def _ask(prompt: str, model: str = "claude-sonnet-4.6", timeout: float = 180.0) -> str:
    """One-shot copilot call. Returns stdout text or "" on failure."""
    try:
        proc = subprocess.run(
            [_copilot_bin(), "-p", prompt, "--model", model, "--no-auto-update"],
            capture_output=True, text=True, timeout=timeout,
            env={**os.environ, "NO_COLOR": "1"},
        )
    except (subprocess.TimeoutExpired, OSError):
        return ""
    out = (proc.stdout or "").strip()
    # strip CLI decoration lines
    lines = [ln for ln in out.split("\n")
             if not ln.startswith("●") and not ln.strip().startswith("└")]
    return "\n".join(lines).strip()


def _digest(data: dict) -> dict:
    """Compact, model-friendly summary of the narrowed signal."""
    tc: Counter = data["tool_counts"]
    tf: Counter = data["tool_fail"]
    sessions = data["by_session"]
    return {
        "scope": data["session_filter"] or "all sessions",
        "sessions": len(sessions),
        "user_messages": data["user_msgs"],
        "tool_calls": sum(tc.values()),
        "top_tools": dict(tc.most_common(10)),
        "tool_failures": dict(tf.most_common(8)),
        "commits": sum(s["commits"] for s in sessions.values()),
        "tasks_completed": sum(s["done"] for s in sessions.values()),
        "bugs_opened": sum(s["bugs"] for s in sessions.values()),
        "median_reply_seconds": round(data["median_response_s"]),
        "correction_clusters": [
            {"topic": c["topic"], "count": c["count"], "examples": c["examples"][:4]}
            for c in data.get("correction_clusters", [])
        ],
        "raw_corrections": [c["text"] for c in data["corrections"][:40]],
        "highlight_moments": [m["text"][:200] for m in data["interesting"][:15]],
    }


_PROMPT_CANDIDATES = """You are analyzing how a specific developer (Ian) works with an AI coding agent \
across many sessions, to improve the agent's base prompt.

Below is mined signal: clusters of messages where Ian *corrected or redirected* the agent, plus raw \
correction snippets. Your job: identify the patterns Ian **repeatedly** has to correct, and turn each \
into a single, imperative base-prompt rule the agent should follow so Ian doesn't have to keep saying it.

Rules for your output:
- Only include patterns with genuine recurrence or clear importance. Ignore one-off design discussion \
that isn't really a correction.
- Each rule must be concrete and actionable, phrased as an instruction to the agent (imperative voice).
- Max 8 rules. Prefer fewer, higher-confidence rules.
- Return ONLY a JSON array of strings. No prose, no markdown fences.

DATA:
{data}
"""

_NARRATIVE = """You are writing a concise, honest "workflow insights" report for a developer (Ian) about \
how he works with his AI coding agents, based on captured session metrics and mined signal.

Write THREE short sections (2-4 sentences each), grounded in the data — no flattery, no invention:
1. "What's working" — genuine strengths visible in the data (cadence, completion, tooling rhythm).
2. "Where the friction is" — what costs extra cycles (tool failures, recurring corrections, etc.).
3. "Quick wins" — 2-3 specific, actionable suggestions tied to the observed patterns.

Return ONLY JSON: {{"sections": [["What's working","..."],["Where the friction is","..."],["Quick wins","..."]]}}
No markdown fences.

DATA:
{data}
"""


def _parse_json(raw: str, default):
    if not raw:
        return default
    s = raw.strip()
    # tolerate code fences
    if s.startswith("```"):
        s = s.split("\n", 1)[-1]
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
    start = s.find("[") if isinstance(default, list) else s.find("{")
    end = s.rfind("]") if isinstance(default, list) else s.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(s[start:end + 1])
        except (ValueError, TypeError):
            return default
    return default


def narrate(data: dict) -> dict:
    """Run the LLM passes; returns {sections, prompt_candidates}. Degrades to {} on failure."""
    digest = _digest(data)
    blob = json.dumps(digest, indent=1, default=str)[:9000]

    candidates = _parse_json(
        _ask(_PROMPT_CANDIDATES.format(data=blob)), default=[]
    )
    sections_obj = _parse_json(
        _ask(_NARRATIVE.format(data=blob)), default={}
    )
    sections = sections_obj.get("sections") if isinstance(sections_obj, dict) else None

    result = {}
    if isinstance(candidates, list) and candidates:
        result["prompt_candidates"] = [str(c) for c in candidates][:8]
    if isinstance(sections, list) and sections:
        result["sections"] = [(str(t), str(b)) for t, b in sections if isinstance(t, str)]
    return result
