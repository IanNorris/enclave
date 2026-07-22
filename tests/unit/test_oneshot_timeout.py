"""Tests for _run_oneshot_warm's activity-based (idle) timeout.

The fusion/grader oneshot must NOT time out while the model is still streaming
(reasoning/message deltas); it should only give up after a genuine idle gap
with no activity. These tests drive a fake SDK session that emits events on a
background thread (mirroring the real SDK) and assert the wait behaviour.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from copilot.generated.session_events import (
    AssistantMessageData,
    SessionEventType,
)

import enclave.agent.main as agent_main


class _FakeEvent:
    def __init__(self, etype, data=None):
        self.type = etype
        self.data = data


class _FakeSession:
    """Emits a scripted sequence of (delay_after_s, event) on send()."""

    def __init__(self, script):
        self.session_id = "fake-sid"
        self._script = script
        self._handlers = []
        self._threads = []

    def on(self, handler):
        self._handlers.append(handler)
        return lambda: self._handlers.remove(handler) if handler in self._handlers else None

    async def set_model(self, *a, **k):
        return None

    async def send(self, prompt, **k):
        def _run():
            for delay, ev in self._script:
                time.sleep(delay)
                for h in list(self._handlers):
                    h(ev)
        t = threading.Thread(target=_run, daemon=True)
        t.start()
        self._threads.append(t)
        return "msg-1"


class _FakeClient:
    def __init__(self, session):
        self._session = session

    async def create_session(self, **k):
        return self._session

    async def delete_session(self, sid):
        return None


def _msg(text):
    return _FakeEvent(
        SessionEventType.ASSISTANT_MESSAGE,
        AssistantMessageData(content=text, message_id="m-1"),
    )


def _delta():
    return _FakeEvent(SessionEventType.ASSISTANT_REASONING_DELTA, None)


def _idle():
    return _FakeEvent(SessionEventType.SESSION_IDLE, None)


@pytest.fixture(autouse=True)
def _fast_idle_gap(monkeypatch):
    # Shrink the idle-gap floor so tests run in well under a second.
    monkeypatch.setattr(agent_main, "_ONESHOT_MIN_IDLE_GAP", 0.3)


@pytest.mark.asyncio
async def test_streaming_activity_prevents_timeout():
    """Deltas spaced under the idle gap, for longer than the idle gap in total,
    must NOT trip the timeout — the final content is returned."""
    # idle_gap = 0.3; emit a delta every 0.15s for ~0.9s (6x), then finish.
    script = [(0.15, _delta()) for _ in range(6)]
    script.append((0.05, _msg("final answer")))
    script.append((0.01, _idle()))
    sess = _FakeSession(script)
    client = _FakeClient(sess)

    result = await agent_main._run_oneshot_warm(client, "m", "p", timeout=0.3)
    assert result == "final answer"


@pytest.mark.asyncio
async def test_idle_gap_trips_timeout():
    """No activity at all → times out after the idle gap with a marker."""
    sess = _FakeSession([])  # emits nothing
    client = _FakeClient(sess)

    result = await agent_main._run_oneshot_warm(client, "m", "p", timeout=0.3)
    assert "inactivity" in result.lower()


@pytest.mark.asyncio
async def test_returns_content_on_clean_idle():
    """A quick message then idle returns the content."""
    sess = _FakeSession([(0.02, _msg("hi")), (0.01, _idle())])
    client = _FakeClient(sess)

    result = await agent_main._run_oneshot_warm(client, "m", "p", timeout=0.3)
    assert result == "hi"


@pytest.mark.asyncio
async def test_stall_after_initial_activity_times_out():
    """Activity then a stall longer than the idle gap → inactivity timeout,
    even though tokens arrived earlier."""
    # two quick deltas, then silence (no idle event) → should time out.
    script = [(0.05, _delta()), (0.05, _delta())]
    sess = _FakeSession(script)
    client = _FakeClient(sess)

    result = await agent_main._run_oneshot_warm(client, "m", "p", timeout=0.3)
    assert "inactivity" in result.lower()


# ── ENC-013: auto-fusion base_model honored by _configure_model ──

class _FakeModel:
    def __init__(self, mid):
        self.id = mid
        self.capabilities = type("C", (), {"supports": type("S", (), {"reasoning_effort": ["low", "medium", "high"]})()})()


class _FakeConfigClient:
    def __init__(self, ids):
        self._ids = ids
    async def list_models(self):
        return [_FakeModel(i) for i in self._ids]


class _FakeConfigSession:
    def __init__(self):
        self.set_model_calls = []
    async def set_model(self, model, **k):
        self.set_model_calls.append((model, k))


@pytest.mark.asyncio
async def test_auto_fusion_base_model_used(tmp_path, monkeypatch):
    import json
    # workspace with a fusion doc that sets base_model
    (tmp_path / ".enclave-fusion.json").write_text(json.dumps({
        "version": 1, "presets": [{"id": "frontier", "participants": [["claude-opus-4.8"]], "enabled": True}], "base_model": "gpt-5.6-terra",
        "base_reasoning_effort": "", "auto_threshold": 4,
    }))
    monkeypatch.setenv("ENCLAVE_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("ENCLAVE_AUTO_FUSION", "1")

    session = _FakeConfigSession()
    # base_model available alongside the normal preferences
    client = _FakeConfigClient(["claude-opus-4.8", "gpt-5.6-terra", "gpt-5.5"])

    await agent_main._configure_model(session, client)

    # The base_model must be the model actually set, not claude-opus-4.8.
    assert session.set_model_calls, "set_model was never called"
    assert session.set_model_calls[0][0] == "gpt-5.6-terra"
    models = json.loads((tmp_path / ".enclave-models.json").read_text())
    assert models["current"] == "gpt-5.6-terra"
    assert models["preferences"][0] == "gpt-5.6-terra"


@pytest.mark.asyncio
async def test_no_auto_fusion_uses_default_preferences(tmp_path, monkeypatch):
    import json
    (tmp_path / ".enclave-fusion.json").write_text(json.dumps({
        "version": 1, "presets": [{"id": "frontier", "participants": [["claude-opus-4.8"]], "enabled": True}], "base_model": "gpt-5.6-terra",
        "base_reasoning_effort": "", "auto_threshold": 4,
    }))
    monkeypatch.setenv("ENCLAVE_WORKSPACE", str(tmp_path))
    monkeypatch.delenv("ENCLAVE_AUTO_FUSION", raising=False)

    session = _FakeConfigSession()
    client = _FakeConfigClient(["claude-opus-4.8", "gpt-5.6-terra"])
    await agent_main._configure_model(session, client)

    # Without auto-fusion, base_model is ignored → default preference wins.
    assert session.set_model_calls[0][0] == "claude-opus-4.8"
