#!/usr/bin/env python3
"""Reconcile a session's live event stream against its durable event store.

This is the feedback loop for the recurring "comes through live but is not
persisted" class of bug. Instead of guessing, it proves — for a real session —
whether every persistable event that streams over the control socket actually
lands in ``events.db``.

Modes
-----
dump (default)
    Summarise events.db: counts by type, time range, staleness, and simple
    anomalies (turns with no persisted agent response, large time gaps).

--live SECONDS
    Subscribe to the control socket for SECONDS, record every persistable event
    seen live, then check each one appears in events.db. Reports:
      * LIVE-BUT-NOT-PERSISTED  -> the bug (data loss)
      * OK                      -> healthy
    Trigger activity (send the agent a message) while this runs.

Examples
--------
    python scripts/inspect_events.py --session brook-8c7de217
    python scripts/inspect_events.py --session brook-8c7de217 --live 30
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_WORKSPACE_BASE = Path("/data/Enclave/workspaces")
DEFAULT_SOCK = Path.home() / ".local/share/enclave/control.sock"

# Must mirror enclave.webui.event_store.PERSIST_TYPES.
PERSIST_TYPES = frozenset({
    "tool_start", "tool_complete", "response", "file_send", "ask_user",
    "user_message", "structured_response",
})


def db_path(workspace_base: Path, session_id: str) -> Path:
    return workspace_base / session_id / ".enclave" / "events.db"


def _connect(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _parse_ts(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def _fingerprint(event_type: str, d: dict) -> tuple[str, str]:
    body = d.get("content") or d.get("summary") or d.get("question") or d.get("name") or ""
    return (event_type, str(body)[:40])


def dump(db: Path) -> None:
    if not db.exists():
        print(f"[!] No events.db at {db}")
        return
    con = _connect(db)
    total = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    print(f"events.db: {db}")
    print(f"  total events : {total}")
    print("  by type      :")
    for r in con.execute("SELECT type, COUNT(*) c FROM events GROUP BY type ORDER BY c DESC"):
        print(f"      {r['type']:<22} {r['c']}")
    row = con.execute("SELECT MIN(timestamp) a, MAX(timestamp) b FROM events").fetchone()
    print(f"  time range   : {row['a']}  ->  {row['b']}")
    last = _parse_ts(row["b"] or "")
    if last:
        age = (datetime.now(timezone.utc) - last).total_seconds()
        flag = "  <-- STALE?" if age > 300 else ""
        print(f"  newest age   : {age:.0f}s{flag}")

    print("  last 10 events:")
    for r in con.execute(
        "SELECT id, type, timestamp, substr(data,1,60) d FROM events ORDER BY id DESC LIMIT 10"
    ):
        print(f"      #{r['id']} {r['timestamp']} {r['type']:<20} {r['d']}")

    print("  anomalies:")
    found = False
    rows = list(con.execute(
        "SELECT id, type FROM events "
        "WHERE type IN ('user_message','response','structured_response','ask_user') ORDER BY id"
    ))
    prev_user = None
    saw_reply = True
    for r in rows:
        if r["type"] == "user_message":
            if prev_user is not None and not saw_reply:
                print(f"      user_message #{prev_user} had no persisted agent reply")
                found = True
            prev_user = r["id"]
            saw_reply = False
        else:
            saw_reply = True
    tprev = None
    for r in con.execute("SELECT id, timestamp FROM events ORDER BY id"):
        t = _parse_ts(r["timestamp"])
        if t and tprev and (t - tprev).total_seconds() > 3600:
            print(f"      >1h gap before #{r['id']} at {r['timestamp']}")
            found = True
        if t:
            tprev = t
    if not found:
        print("      (none)")


async def live(db: Path, sock: Path, session_id: str, seconds: int) -> int:
    print(f"[*] Subscribing to {session_id} for {seconds}s -- trigger activity now...")
    reader, writer = await asyncio.open_unix_connection(str(sock))
    writer.write(json.dumps({"action": "subscribe", "session": session_id}).encode() + b"\n")
    await writer.drain()

    seen: list[dict] = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            line = await asyncio.wait_for(
                reader.readline(), timeout=max(0.5, deadline - time.monotonic())
            )
        except asyncio.TimeoutError:
            break
        if not line:
            break
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        if evt.get("type") in PERSIST_TYPES:
            seen.append(evt)
            print(f"    live: {evt.get('type')}")
    writer.close()

    print(f"[*] Saw {len(seen)} persistable live events. Reconciling with events.db...")
    await asyncio.sleep(2)  # allow source-side write to flush

    if not db.exists():
        print(f"[!] events.db missing at {db} -- ALL {len(seen)} live events lost")
        return 1

    con = _connect(db)
    recent: dict[tuple[str, str], int] = {}
    for r in con.execute("SELECT type, data FROM events ORDER BY id DESC LIMIT 500"):
        try:
            d = json.loads(r["data"])
        except Exception:
            d = {}
        key = _fingerprint(r["type"], d)
        recent[key] = recent.get(key, 0) + 1

    missing = 0
    for evt in seen:
        key = _fingerprint(evt["type"], evt)
        if recent.get(key, 0) > 0:
            recent[key] -= 1
        else:
            missing += 1
            print(f"    LIVE-BUT-NOT-PERSISTED: {evt['type']} {str(evt)[:80]}")

    if missing == 0:
        print(f"[OK] All {len(seen)} live persistable events are durably persisted.")
        return 0
    print(f"[FAIL] {missing}/{len(seen)} live events were NOT persisted (data loss).")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--session", required=True, help="session id, e.g. brook-8c7de217")
    ap.add_argument("--workspace-base", type=Path, default=DEFAULT_WORKSPACE_BASE)
    ap.add_argument("--sock", type=Path, default=DEFAULT_SOCK)
    ap.add_argument("--live", type=int, metavar="SECONDS",
                    help="reconcile live stream vs events.db")
    args = ap.parse_args()

    db = db_path(args.workspace_base, args.session)
    if args.live:
        return asyncio.run(live(db, args.sock, args.session, args.live))
    dump(db)
    return 0


if __name__ == "__main__":
    sys.exit(main())
