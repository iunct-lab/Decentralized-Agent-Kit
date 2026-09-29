import time

from dak_agent.approvals import (
    PENDING_TIMEOUT_SECONDS,
    build_reply_function_response,
    is_expired,
    list_pending_approvals,
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
