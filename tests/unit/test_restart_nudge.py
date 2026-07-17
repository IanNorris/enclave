"""Tests for the restart-resume nudge persistence (ENC-008).

A session that is mid-turn when the orchestrator restarts must persist a
``pending_restart_nudge`` marker so the router can wake it to resume its
interrupted work once the agent reports ready. A session that is not
mid-turn must load with the marker clear, so idle sessions are not spuriously
nudged.
"""

from __future__ import annotations

import asyncio
import tempfile

from enclave.common.config import ContainerConfig
from enclave.orchestrator.session_manager import SessionManager


def _make_manager() -> SessionManager:
    tmp = tempfile.mkdtemp()
    cfg = ContainerConfig(
        workspace_base=tmp + "/ws",
        session_base=tmp + "/sess",
        socket_dir=tmp + "/sock",
    )
    return SessionManager(config=cfg)


def test_restart_nudge_flag_round_trips() -> None:
    sm = _make_manager()
    session = asyncio.run(
        sm.create_session(
            name="Worker", room_id="!r:test", socket_path="/tmp/s.sock",
        )
    )
    session.status = "running"
    session.pending_restart_nudge = True
    sm.save_sessions()

    # Reload with a fresh manager pointed at the same dirs.
    sm2 = SessionManager(config=sm.config)
    loaded = sm2.get_session(session.id)
    assert loaded is not None
    # Running + mid-turn: restores as was_running with the marker intact.
    assert loaded.status == "was_running"
    assert loaded.pending_restart_nudge is True


def test_restart_nudge_defaults_clear() -> None:
    sm = _make_manager()
    session = asyncio.run(
        sm.create_session(
            name="Worker", room_id="!r:test", socket_path="/tmp/s.sock",
        )
    )
    session.status = "running"
    sm.save_sessions()

    sm2 = SessionManager(config=sm.config)
    loaded = sm2.get_session(session.id)
    assert loaded is not None
    assert loaded.pending_restart_nudge is False
