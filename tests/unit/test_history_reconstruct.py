"""Unit tests for chat history reconstruction from the event store."""

from __future__ import annotations

from enclave.webui.routes.chat import _reconstruct_turns


def _evt(etype: str, ts: str, **data):
    return {"type": etype, "timestamp": ts, "data": data}


def test_basic_user_then_responses_groups_into_one_turn():
    events = [
        _evt("user_message", "t1", content="hello"),
        _evt("response", "t2", content="first"),
        _evt("response", "t3", content="final"),
    ]
    turns = _reconstruct_turns(events)
    assert len(turns) == 1
    assert turns[0]["user_message"] == "hello"
    # assistant_response is the turn's final major output
    assert turns[0]["assistant_response"] == "final"
    assert turns[0]["turn_index"] == 0
    assert turns[0]["timestamp"] == "t1"


def test_multiple_turns_get_sequential_indexes():
    events = [
        _evt("user_message", "t1", content="q1"),
        _evt("response", "t2", content="a1"),
        _evt("user_message", "t3", content="q2"),
        _evt("response", "t4", content="a2"),
    ]
    turns = _reconstruct_turns(events)
    assert [t["turn_index"] for t in turns] == [0, 1]
    assert turns[1]["user_message"] == "q2"
    assert turns[1]["assistant_response"] == "a2"


def test_leading_agent_output_forms_anonymous_turn():
    events = [
        _evt("response", "t1", content="autonomous"),
        _evt("user_message", "t2", content="hi"),
        _evt("response", "t3", content="reply"),
    ]
    turns = _reconstruct_turns(events)
    assert len(turns) == 2
    assert turns[0]["user_message"] is None
    assert turns[0]["assistant_response"] == "autonomous"
    assert turns[1]["user_message"] == "hi"


def test_ask_user_is_surfaced_as_response():
    events = [
        _evt("user_message", "t1", content="do it"),
        _evt("ask_user", "t2", question="Which one?"),
    ]
    turns = _reconstruct_turns(events)
    assert turns[0]["assistant_response"] == "**Question:** Which one?"


def test_structured_response_creates_turn_without_overwriting_response():
    # A structured_response alone (no plain response) still yields a turn so the
    # client can map the card by timestamp; assistant_response stays None.
    events = [
        _evt("user_message", "t1", content="make a card"),
        _evt("structured_response", "t2", summary="card summary"),
    ]
    turns = _reconstruct_turns(events)
    assert len(turns) == 1
    assert turns[0]["assistant_response"] is None


def test_empty_events_yields_no_turns():
    assert _reconstruct_turns([]) == []


def test_json_string_data_is_parsed():
    import json

    events = [
        {"type": "user_message", "timestamp": "t1", "data": json.dumps({"content": "hi"})},
        {"type": "response", "timestamp": "t2", "data": json.dumps({"content": "yo"})},
    ]
    turns = _reconstruct_turns(events)
    assert turns[0]["user_message"] == "hi"
    assert turns[0]["assistant_response"] == "yo"
