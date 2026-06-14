#!/usr/bin/env python3
"""Enclave Insights — workflow analytics from captured session data.

Generates a self-contained HTML report (Claude-Code-Insights style) from the
data Enclave already records: per-session event stores, the cost tracker, and
the bug tracker. Two layers:

1. **Metrics** — deterministic SQL/Python over the DBs (exact tool/turn counts,
   cadence, failure rates). No LLM.
2. **Signal narrowing** — reduce 60k+ events to the interesting few: interaction
   boundaries (user replies), celebration/milestone moments, and — the headline
   feature — user *corrections* that recur, surfaced as base-prompt candidates.
3. **Narrative** (optional, --narrate) — an LLM pass over the *narrowed signal*
   (not raw events) writes the prose sections. Runs through the bundled copilot
   CLI, same path as everything else.

Usage:
    python scripts/insights.py --all -o report.html [--narrate]
    python scripts/insights.py --session brook-8c7de217 -o report.html
"""

from __future__ import annotations

import argparse
import glob
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

WORKSPACES = Path("/data/Enclave/workspaces")
DATA_DIR = Path.home() / ".local/share/enclave"

# ─── Signal patterns ────────────────────────────────────────────────────────

# Celebration / accomplishment markers in agent output.
_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF]|[\u2728\u2705\u2b50\u2714\U0001F389\U0001F38A\U0001F44D\U0001F525]"
)
_WIN_WORDS = re.compile(
    r"\b(breakthrough|milestone|achievement|root cause|fixed it|solved|"
    r"works now|working now|nailed it|verified|confirmed working|"
    r"end[- ]to[- ]end|all (?:tests?|checks) pass)\b",
    re.I,
)

# Strong, low-false-positive *correction* signals from the USER. Deliberately
# excludes bare "actually" (mostly conversational) — requires an imperative or
# negation that implies the agent did/assumed something wrong.
_CORRECTION = re.compile(
    r"(?:^|\b)(?:no[,.]| not quite| that'?s not (?:right|what|it)| that'?s wrong|"
    r"don'?t |do not | instead of | should (?:be|have|use|not)| "
    r"i (?:meant|said|asked)| revert| undo| roll ?back| stop (?:doing|using)| "
    r"why (?:did|are) you| you (?:shouldn'?t|missed|forgot|broke)| "
    r"that broke| didn'?t work| doesn'?t work| not what i)",
    re.I,
)

# Coarse correction *topics* — what kind of thing is being corrected. Used to
# cluster recurring corrections into base-prompt candidates.
_TOPIC_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("scope / over-reach", re.compile(r"\b(scope|only|just the|don'?t (?:also|touch|change)|too (?:much|many)|unrelated)\b", re.I)),
    ("model selection", re.compile(r"\b(model|opus|sonnet|gpt|haiku|gemini|4\.\d|fusion)\b", re.I)),
    ("verification / testing", re.compile(r"\b(test|verify|compile|build|check|confirm|prove|validate)\b", re.I)),
    ("destructive / safety", re.compile(r"\b(kill|pkill|rm |delete|drop|destroy|wipe|force)\b", re.I)),
    ("commit / git workflow", re.compile(r"\b(commit|branch|rebase|push|pr |merge|git)\b", re.I)),
    ("assumptions / verify-first", re.compile(r"\b(assume|exist|doesn'?t exist|made up|hallucinat|guess)\b", re.I)),
    ("communication / verbosity", re.compile(r"\b(concise|shorter|too long|verbose|summar|brief|don'?t explain)\b", re.I)),
    ("restart / deploy hygiene", re.compile(r"\b(restart|deploy|bounce|rebuild|reload|image)\b", re.I)),
]

_RESP_TYPES = ("response", "structured_response")


def _utc(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _event_dbs(session: str | None) -> list[tuple[str, Path]]:
    """Return (session_id, events.db path) pairs."""
    out = []
    for db in sorted(WORKSPACES.glob("*/.enclave/events.db")):
        sess = str(db).split("/workspaces/")[1].split("/")[0]
        if session and sess != session:
            continue
        out.append((sess, db))
    return out


def _text_of(data: dict) -> str:
    return data.get("content") or data.get("summary") or data.get("title") or ""


# ─── Metrics layer ──────────────────────────────────────────────────────────


def collect(session: str | None) -> dict[str, Any]:
    """Walk the event stores and compute metrics + narrowed signal."""
    tool_counts: Counter[str] = Counter()
    tool_fail: Counter[str] = Counter()
    lang_counts: Counter[str] = Counter()
    by_session: dict[str, dict[str, Any]] = {}
    interesting: list[dict[str, Any]] = []
    corrections: list[dict[str, Any]] = []
    user_msgs = 0
    response_gaps: list[float] = []
    hour_hist: Counter[int] = Counter()
    first_ts = last_ts = None

    for sess, db in _event_dbs(session):
        try:
            conn = sqlite3.connect(str(db))
            rows = conn.execute(
                "SELECT type, timestamp, data FROM events ORDER BY id"
            ).fetchall()
            conn.close()
        except sqlite3.Error:
            continue

        s = by_session.setdefault(
            sess,
            {"tools": Counter(), "tool_fail": 0, "tool_total": 0, "user_msgs": 0,
             "responses": 0, "files": set(), "commits": 0, "bugs": 0,
             "interesting": 0, "corrections": 0, "first": None, "last": None,
             "done": 0, "asks": 0},
        )
        last_user_ts: datetime | None = None

        for etype, ts, data in rows:
            dt = _utc(ts)
            if dt:
                if first_ts is None or dt < first_ts:
                    first_ts = dt
                if last_ts is None or dt > last_ts:
                    last_ts = dt
                if s["first"] is None or dt < s["first"]:
                    s["first"] = dt
                if s["last"] is None or dt > s["last"]:
                    s["last"] = dt
            try:
                j = json.loads(data)
            except (ValueError, TypeError):
                j = {}

            if etype == "tool_start":
                name = (j.get("name") or j.get("detail") or "?").split()[0]
                tool_counts[name] += 1
                s["tools"][name] += 1
                s["tool_total"] += 1
                if name in ("git_commit",):
                    s["commits"] += 1
                if name == "mark_done":
                    s["done"] += 1
                if name in ("ask_user", "ask_deferred"):
                    s["asks"] += 1
                if name == "bug_open":
                    s["bugs"] += 1
                # language by file extension in edit/create/view targets
                tgt = j.get("detail") or ""
                m = re.search(r"\.([a-zA-Z0-9]{1,5})\b", tgt)
                if m and name in ("edit", "create", "view"):
                    lang_counts[m.group(1).lower()] += 1
            elif etype == "tool_complete":
                if j.get("success") is False:
                    name = (j.get("tool_name") or "?").split()[0]
                    tool_fail[name] += 1
                    s["tool_fail"] += 1
            elif etype == "user_message":
                user_msgs += 1
                s["user_msgs"] += 1
                txt = _text_of(j)
                if dt:
                    hour_hist[dt.hour] += 1
                    if last_user_ts:
                        gap = (dt - last_user_ts).total_seconds()
                        if 0 < gap < 3600:
                            response_gaps.append(gap)
                    last_user_ts = dt
                # correction mining (interaction-boundary, high-signal)
                snippet = txt.strip()[:300]
                if snippet and _CORRECTION.search(snippet) and not _is_question(snippet):
                    topics = [name for name, rx in _TOPIC_RULES if rx.search(snippet)]
                    corrections.append({"session": sess, "text": snippet, "topics": topics})
                    s["corrections"] += 1
            elif etype in _RESP_TYPES:
                s["responses"] += 1
                txt = _text_of(j)
                if _EMOJI.search(txt) or _WIN_WORDS.search(txt):
                    s["interesting"] += 1
                    if len(interesting) < 400:
                        interesting.append({"session": sess, "text": txt.strip()[:400]})

    # roll up
    median_gap = _median(response_gaps)
    return {
        "session_filter": session,
        "tool_counts": tool_counts,
        "tool_fail": tool_fail,
        "lang_counts": lang_counts,
        "by_session": by_session,
        "interesting": interesting,
        "corrections": corrections,
        "user_msgs": user_msgs,
        "median_response_s": median_gap,
        "hour_hist": hour_hist,
        "first_ts": first_ts,
        "last_ts": last_ts,
    }


def _is_question(txt: str) -> bool:
    """Heuristic: a question to the agent isn't a correction."""
    t = txt.strip()
    return t.endswith("?") and not _CORRECTION.search(t[:40])


def _median(xs: list[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


# ─── Correction clustering → base-prompt candidates ─────────────────────────


def cluster_corrections(corrections: list[dict]) -> list[dict]:
    """Group corrections by topic; surface recurring ones as prompt candidates."""
    by_topic: dict[str, list[str]] = defaultdict(list)
    for c in corrections:
        for topic in (c["topics"] or ["other"]):
            by_topic[topic].append(c["text"])
    clusters = []
    for topic, texts in by_topic.items():
        if topic == "other" or len(texts) < 2:
            continue
        clusters.append({"topic": topic, "count": len(texts), "examples": texts[:4]})
    clusters.sort(key=lambda c: -c["count"])
    return clusters


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Enclave workflow insights report")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--all", action="store_true", help="All sessions")
    g.add_argument("--session", help="Single session id")
    ap.add_argument("-o", "--output", default="insights.html")
    ap.add_argument("--narrate", action="store_true", help="LLM narrative pass")
    ap.add_argument("--json", action="store_true", help="Dump metrics JSON to stdout")
    args = ap.parse_args(argv)

    data = collect(None if args.all else args.session)
    data["correction_clusters"] = cluster_corrections(data["corrections"])

    if args.json:
        print(json.dumps(_jsonable(data), indent=2, default=str)[:8000])
        return 0

    narrative = {}
    if args.narrate:
        from insights_narrate import narrate  # local module
        narrative = narrate(data)

    from insights_render import render_html
    html = render_html(data, narrative)
    Path(args.output).write_text(html, encoding="utf-8")
    print(f"Wrote {args.output} ({len(html)} bytes)")
    print(f"  sessions={len(data['by_session'])} tools={sum(data['tool_counts'].values())} "
          f"corrections={len(data['corrections'])} clusters={len(data['correction_clusters'])}")
    return 0


def _jsonable(d: dict) -> dict:
    out = {}
    for k, v in d.items():
        if isinstance(v, Counter):
            out[k] = dict(v.most_common())
        elif isinstance(v, dict) and k == "by_session":
            out[k] = {s: {kk: (dict(vv.most_common()) if isinstance(vv, Counter)
                              else (len(vv) if isinstance(vv, set) else str(vv) if hasattr(vv, "isoformat") else vv))
                          for kk, vv in sv.items()} for s, sv in v.items()}
        else:
            out[k] = v
    return out


# render_html is imported lazily inside main() after sys.path is set.


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent))
    raise SystemExit(main())
