"""Durable persistence happens at the source (orchestrator control socket).

The control server's ``_emit`` is the single chokepoint every agent event
funnels through. Persisting there (rather than in a downstream subscriber)
guarantees exactly-once capture regardless of whether any browser or the
webui persister is connected — closing the lossy "live but not persisted"
window.
"""
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from enclave.orchestrator.control import ControlServer
from enclave.webui.event_store import get_event_store


def _server(tmp_path: Path, session_id: str) -> ControlServer:
    workspace_path = tmp_path / session_id  # base is tmp_path
    sess = SimpleNamespace(id=session_id, workspace_path=str(workspace_path))
    router = SimpleNamespace(
        sessions=SimpleNamespace(get_session=lambda sid: sess if sid == session_id else None)
    )
    return ControlServer(tmp_path / "control.sock", router)


def _rows(tmp_path: Path, session_id: str):
    # Use a fresh connection so we observe the committed rows.
    db = tmp_path / session_id / ".enclave" / "events.db"
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return list(con.execute("SELECT type, data FROM events ORDER BY id"))
    finally:
        con.close()


def test_persistable_events_written_without_any_subscriber(tmp_path):
    sid = "sess-1"
    srv = _server(tmp_path, sid)

    # No subscribers attached at all — this is exactly the historically lossy case.
    srv.notify_response(sid, "the final answer")
    srv.notify_tool_start(sid, "view", "/x")
    srv.notify_structured_response(sid, {"title": "T", "summary": "major update"})

    rows = _rows(tmp_path, sid)
    types = [r[0] for r in rows]
    assert types == ["response", "tool_start", "structured_response"]


def test_streaming_types_are_not_persisted(tmp_path):
    sid = "sess-2"
    srv = _server(tmp_path, sid)
    srv.notify_delta(sid, "partial")
    srv.notify_thinking(sid, "hmm")
    srv.notify_activity(sid, "doing things")
    srv.notify_turn_start(sid)
    db = tmp_path / sid / ".enclave" / "events.db"
    assert not db.exists() or _rows(tmp_path, sid) == []


def test_emit_still_fans_out_to_subscribers(tmp_path):
    import asyncio

    sid = "sess-3"
    srv = _server(tmp_path, sid)
    q: asyncio.Queue = asyncio.Queue()
    srv._subscribers[sid] = {q}
    srv.notify_response(sid, "hi")
    evt = q.get_nowait()
    assert evt["type"] == "response" and evt["content"] == "hi"


def test_unknown_session_does_not_raise(tmp_path):
    srv = _server(tmp_path, "known")
    # Session not found → no workspace, no write, no crash.
    srv.notify_response("ghost", "lost?")


def test_file_send_persists_size_for_download(tmp_path):
    """A non-image file_send persists its size so the web UI can show it and
    offer a download (ENC-010)."""
    import json

    sid = "sess-file"
    srv = _server(tmp_path, sid)

    srv.notify_file_send(
        sid,
        filename="enclave-debug.apk",
        mimetype="application/vnd.android.package-archive",
        file_path="/ws/enclave-debug.apk",
        size=4_900_123,
    )

    rows = _rows(tmp_path, sid)
    assert [r[0] for r in rows] == ["file_send"]
    data = json.loads(rows[0][1])
    assert data["size"] == 4_900_123
    assert data["filename"] == "enclave-debug.apk"
    assert data["file_path"] == "/ws/enclave-debug.apk"
