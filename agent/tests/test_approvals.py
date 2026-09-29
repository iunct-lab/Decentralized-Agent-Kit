import time

import pytest

from dak_agent.approvals import (
    PENDING_TIMEOUT_SECONDS,
    build_question_reply,
    build_reply_function_response,
    is_expired,
    list_pending,
    list_pending_approvals,
    list_pending_questions,
)


def _user_text(text, ts=1.0):
    return {"author": "user", "timestamp": ts, "content": {"role": "user", "parts": [{"text": text}]}}


def _confirmation_call(fc_id="adk-1", ts=2.0):
    """The event ADK emits when a tool asks for confirmation (functions.py:402-444)."""
    return {
        "author": "dak_agent",
        "timestamp": ts,
        "longRunningToolIds": [fc_id],
        "content": {
            "role": "model",
            "parts": [{
                "functionCall": {
                    "id": fc_id,
                    "name": "adk_request_confirmation",
                    "args": {
                        "originalFunctionCall": {"id": "call-1", "name": "planner", "args": {"task_description": "t"}},
                        "toolConfirmation": {"hint": "Please approve", "confirmed": False},
                    },
                },
            }],
        },
    }


def _confirmation_answer(fc_id="adk-1", ts=3.0):
    return {
        "author": "user",
        "timestamp": ts,
        "content": {"role": "user", "parts": [{
            "functionResponse": {"id": fc_id, "name": "adk_request_confirmation", "response": {"confirmed": True}},
        }]},
    }


def test_list_pending_finds_unanswered_confirmation():
    pending = list_pending_approvals([_user_text("plan it"), _confirmation_call()])
    assert pending == [{
        "id": "adk-1",
        "kind": "approval",
        "tool_name": "planner",
        "tool_args": {"task_description": "t"},
        "hint": "Please approve",
        "requested_at": 2.0,
    }]


def test_list_pending_excludes_answered():
    events = [_user_text("plan it"), _confirmation_call(), _confirmation_answer()]
    assert list_pending_approvals(events) == []


def test_list_pending_excludes_confirmation_abandoned_by_new_message():
    """ADK only reads confirmations from the last user event, so a new message drops the old one."""
    events = [_user_text("plan it"), _confirmation_call(), _user_text("never mind", ts=4.0)]
    assert list_pending_approvals(events) == []


def test_build_reply_once():
    message = build_reply_function_response("adk-1", "once")
    fr = message["parts"][0]["functionResponse"]
    assert fr["id"] == "adk-1"
    assert fr["name"] == "adk_request_confirmation"
    assert fr["response"] == {"confirmed": True, "payload": {"mode": "once", "reason": ""}}


def test_build_reply_reject_carries_reason():
    message = build_reply_function_response("adk-1", "reject", "not now")
    response = message["parts"][0]["functionResponse"]["response"]
    assert response["confirmed"] is False
    assert response["payload"]["reason"] == "not now"


def test_build_reply_refuses_unknown_mode():
    """A typo must not turn into a rejection without a reason."""
    with pytest.raises(ValueError):
        build_reply_function_response("adk-1", "approve")


def test_is_expired():
    assert is_expired(time.time() - PENDING_TIMEOUT_SECONDS - 1)
    assert not is_expired(time.time())


def test_list_pending_reads_dumped_adk_events():
    """The dict shape above is what ADK's own Event dumps to."""
    from google.adk.events.event import Event
    from google.genai import types

    call = _confirmation_call()["content"]["parts"][0]["functionCall"]
    events = [
        Event(author="user", content=types.Content(role="user", parts=[types.Part(text="plan it")])),
        Event(author="dak_agent", content=types.Content(role="model", parts=[
            types.Part(function_call=types.FunctionCall(**call))])),
    ]
    dumped = [e.model_dump(mode="json", by_alias=True, exclude_none=True) for e in events]
    [pending] = list_pending_approvals(dumped)
    assert pending["id"] == "adk-1"
    assert pending["tool_name"] == "planner"
    assert pending["requested_at"] == events[1].timestamp


def _compaction(ts=2.5):
    """The event DAK's summarizer appends after an invocation (harness._compaction_event): author "user", no content."""
    from google.adk.events.event import Event
    from google.genai import types

    from dak_agent.harness import BudgetedEventSummarizer

    source = [Event(author="user", timestamp=1.0), Event(author="dak_agent", timestamp=ts)]
    event = BudgetedEventSummarizer._compaction_event(
        source, types.Content(role="model", parts=[types.Part(text="summary")]), None)
    event.timestamp = ts
    return event.model_dump(mode="json", by_alias=True, exclude_none=True)


def test_compaction_after_the_request_keeps_it_pending():
    """The compaction event is authored "user" but is not an answer: the request stays pending."""
    events = [_user_text("plan it"), _confirmation_call(), _compaction(ts=2.5)]
    assert [p["id"] for p in list_pending_approvals(events)] == ["adk-1"]


def test_compaction_does_not_revive_an_answered_request():
    events = [_user_text("plan it"), _confirmation_call(), _confirmation_answer(), _compaction(ts=3.5)]
    assert list_pending_approvals(events) == []


def _question_call(fc_id="call-q", ts=5.0):
    """`ask_question` ends the invocation right after its own response."""
    args = {"questions": ["Which branch?"], "context": "Two branches match"}
    return [
        {"author": "dak_agent", "timestamp": ts, "content": {"role": "model", "parts": [
            {"functionCall": {"id": fc_id, "name": "ask_question", "args": args}}]}},
        {"author": "dak_agent", "timestamp": ts + 0.1, "content": {"role": "user", "parts": [
            {"functionResponse": {"id": fc_id, "name": "ask_question", "response": {"result": "Questions for user"}}}]}},
    ]


def test_list_pending_includes_open_question():
    pending = list_pending_questions([_user_text("deploy it"), *_question_call()])
    assert pending == [{
        "id": "call-q",
        "kind": "question",
        "tool_name": "ask_question",
        "questions": ["Which branch?"],
        "context": "Two branches match",
        "requested_at": 5.0,
    }]


def test_list_pending_excludes_answered_question():
    events = [_user_text("deploy it"), *_question_call(), _user_text("main", ts=6.0)]
    assert list_pending_questions(events) == []


def test_failed_question_is_not_pending():
    """A call ADK rejected (missing argument) did not end the invocation, so
    nobody is waiting for an answer to it."""
    call, response = _question_call()
    response["content"]["parts"][0]["functionResponse"]["response"] = {"error": "Invoking `ask_question()` failed"}
    answer = {"author": "dak_agent", "timestamp": 6.0, "content": {"role": "model", "parts": [{"text": "done"}]}}
    assert list_pending_questions([_user_text("deploy it"), call, response]) == []
    assert list_pending_questions([_user_text("deploy it"), call, response, answer]) == []


def test_only_the_question_that_ended_the_invocation_is_pending():
    first_call, first_error = _question_call("call-bad")
    first_error["content"]["parts"][0]["functionResponse"]["response"] = {"error": "missing context"}
    events = [_user_text("deploy it"), first_call, first_error, *_question_call("call-q", ts=7.0)]
    assert [p["id"] for p in list_pending_questions(events)] == ["call-q"]


def test_list_pending_includes_both_kinds():
    assert [p["kind"] for p in list_pending([_user_text("go"), *_question_call()])] == ["question"]
    assert [p["kind"] for p in list_pending([_user_text("go"), _confirmation_call()])] == ["approval"]


def test_list_pending_orders_approvals_and_questions_by_time():
    """One model turn calls both ask_question (1.5) and a confirmed tool (2.0):
    the question was asked first, though approvals are collected first."""
    question, response = _question_call(ts=1.5)
    events = [_user_text("go"), question, _confirmation_call(ts=2.0), {**response, "timestamp": 2.1}]
    assert [(p["kind"], p["id"]) for p in list_pending(events)] == [("question", "call-q"), ("approval", "adk-1")]


def test_compaction_after_the_question_keeps_it_pending():
    """The compaction event comes after the question's response, which must still count as the last event."""
    events = [_user_text("deploy it"), *_question_call(), _compaction(ts=6.0)]
    assert [p["id"] for p in list_pending_questions(events)] == ["call-q"]


def test_build_question_reply_is_a_plain_message():
    assert build_question_reply("main") == {"parts": [{"text": "main"}]}
