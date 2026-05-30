"""Major (structured) updates must reach Matrix immediately, unclobbered.

Regression: structured responses were buffered into the single shared
``_response_buffer``; trailing regular responses overwrote it before turn_end
(and task_done/ask_user discarded it), so major updates often never reached
Matrix. They are now sent immediately on receipt.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from enclave.orchestrator.router import MessageRouter


def _make_self():
    return SimpleNamespace(
        _control=MagicMock(),
        matrix=SimpleNamespace(send_message=AsyncMock(return_value="$evt")),
        _thread_events={"s1": "$thread"},
        _response_buffer={},
        _response_buffer_thread={},
        _response_flush_tasks={},
    )


def _session():
    return SimpleNamespace(id="s1", room_id="!room:server")


def test_structured_response_sent_immediately():
    me = _make_self()
    session = _session()
    msg = SimpleNamespace(payload={"title": "Done", "summary": "Fixed BRO-154"})

    asyncio.run(MessageRouter._handle_structured_response(me, session, msg))

    me._control.notify_structured_response.assert_called_once()
    me.matrix.send_message.assert_awaited_once()
    args, kwargs = me.matrix.send_message.call_args
    assert args[0] == "!room:server"
    body = args[1]
    assert "Done" in body and "Fixed BRO-154" in body
    assert kwargs.get("thread_event_id") == "$thread"


def test_structured_response_discards_buffered_intermediate():
    me = _make_self()
    session = _session()
    # An intermediate chatty response is buffered and pending a flush.
    me._response_buffer["s1"] = "Let me read the locking docs..."
    me._response_buffer_thread["s1"] = "$thread"
    pending = MagicMock()
    pending.done.return_value = False
    me._response_flush_tasks["s1"] = pending

    msg = SimpleNamespace(payload={"title": "T", "summary": "Major update"})
    asyncio.run(MessageRouter._handle_structured_response(me, session, msg))

    # The intermediate buffer is discarded (superseded by the major update)
    # and its flush timer cancelled, so it cannot post stale chatter later.
    assert "s1" not in me._response_buffer
    assert "s1" not in me._response_buffer_thread
    assert "s1" not in me._response_flush_tasks
    pending.cancel.assert_called_once()
    me.matrix.send_message.assert_awaited_once()


def test_structured_response_without_summary_is_ignored():
    me = _make_self()
    session = _session()
    msg = SimpleNamespace(payload={"title": "T"})
    asyncio.run(MessageRouter._handle_structured_response(me, session, msg))
    me.matrix.send_message.assert_not_awaited()
