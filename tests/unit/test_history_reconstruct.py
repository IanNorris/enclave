"""Unit tests for chat history reconstruction from the event store."""

from __future__ import annotations

from enclave.webui.routes.chat import _reconstruct_turns


def _evt(etype: str, ts: str, **data):
    return {"type": etype, "timestamp": ts, "data": data}


def test_first_response_attaches_extra_responses_split_into_own_turns():
    # Every agent response must survive reload. The first attaches to the user
    # turn; each subsequent one becomes its own (anonymous) bubble so none are
    # lost the way the old _major[-1] collapse dropped them.
    events = [
        _evt("user_message", "t1", content="hello"),
        _evt("response", "t2", content="first"),
        _evt("response", "t3", content="middle"),
        _evt("response", "t4", content="final"),
    ]
    turns = _reconstruct_turns(events)
    assert len(turns) == 3
    assert turns[0]["user_message"] == "hello"
    assert turns[0]["assistant_response"] == "first"
    assert turns[1]["user_message"] is None
    assert turns[1]["assistant_response"] == "middle"
    assert turns[2]["user_message"] is None
    assert turns[2]["assistant_response"] == "final"
    assert [t["turn_index"] for t in turns] == [0, 1, 2]
    assert turns[0]["timestamp"] == "t1"
    assert turns[1]["timestamp"] == "t3"


def test_single_response_turn_unchanged():
    events = [
        _evt("user_message", "t1", content="hello"),
        _evt("response", "t2", content="only"),
    ]
    turns = _reconstruct_turns(events)
    assert len(turns) == 1
    assert turns[0]["user_message"] == "hello"
    assert turns[0]["assistant_response"] == "only"


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
    assert turns[1]["assistant_response"] == "reply"


def test_ask_user_is_surfaced_as_response():
    events = [
        _evt("user_message", "t1", content="do it"),
        _evt("ask_user", "t2", question="Which one?"),
    ]
    turns = _reconstruct_turns(events)
    assert turns[0]["assistant_response"] == "**Question:** Which one?"


def test_structured_response_is_its_own_card_turn():
    # A structured_response becomes a dedicated, server-authoritative card turn
    # carrying the full payload — it never overwrites a regular response.
    events = [
        _evt("user_message", "t1", content="make a card"),
        _evt("response", "t2", content="working on it"),
        _evt("structured_response", "t3", title="Done", summary="card summary"),
    ]
    turns = _reconstruct_turns(events)
    assert len(turns) == 2
    assert turns[0]["assistant_response"] == "working on it"
    assert "structured" not in turns[0]
    assert turns[1]["assistant_response"] is None
    assert turns[1]["structured"] == {"title": "Done", "summary": "card summary"}
    assert turns[1]["is_major"] is True


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
