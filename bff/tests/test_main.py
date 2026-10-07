"""Unit tests for the BFF. The agent API is mocked with respx."""
import json
import os
import sys

import httpx
import respx
from fastapi.testclient import TestClient
from httpx import Response

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main
from main import AGENT_URL, app

client = TestClient(app)

SESSION_ID = "session_bff_test"
USER_ID = "user_session_bff_test"


def post_chat(prompt: str = "hello"):
    return client.post(
        "/chat",
        data={"prompt": prompt, "session_id": SESSION_ID, "user_id": USER_ID},
    )


def adk_text_event(text: str) -> dict:
    return {"content": {"parts": [{"text": text}]}}


def test_index_returns_chat_page():
    response = client.get("/")
    assert response.status_code == 200
    assert "session_bff_" in response.text


@respx.mock
def test_chat_renders_agent_answer():
    respx.get(f"{AGENT_URL}/apps/dak_agent/users/{USER_ID}/sessions/{SESSION_ID}").mock(
        return_value=Response(200, json={"id": SESSION_ID})
    )
    respx.post(f"{AGENT_URL}/run").mock(
        return_value=Response(200, json=[adk_text_event("Hello from the agent!")])
    )

    response = post_chat("hello")

    assert response.status_code == 200
    assert "Hello from the agent!" in response.text
    # The user prompt is echoed back into the chat log
    assert 'class="chat-message user"' in response.text


@respx.mock
def test_chat_creates_session_when_missing():
    respx.get(f"{AGENT_URL}/apps/dak_agent/users/{USER_ID}/sessions/{SESSION_ID}").mock(
        return_value=Response(404)
    )
    create_route = respx.post(f"{AGENT_URL}/apps/dak_agent/users/{USER_ID}/sessions").mock(
        return_value=Response(200, json={"id": "session_new"})
    )
    respx.post(f"{AGENT_URL}/run").mock(
        return_value=Response(200, json=[adk_text_event("ok")])
    )

    response = post_chat()

    assert response.status_code == 200
    assert create_route.called
    # The new session id is pushed back to the client via an OOB swap
    assert 'value="session_new"' in response.text


@respx.mock
def test_chat_renders_tool_calls_as_thoughts():
    respx.get(f"{AGENT_URL}/apps/dak_agent/users/{USER_ID}/sessions/{SESSION_ID}").mock(
        return_value=Response(200, json={"id": SESSION_ID})
    )
    events = [
        {"content": {"parts": [{"functionCall": {"name": "read_file", "args": {"path": "x"}}}]}},
        {"content": {"parts": [{"functionResponse": {"name": "read_file", "response": {"result": "data"}}}]}},
        adk_text_event("done"),
    ]
    respx.post(f"{AGENT_URL}/run").mock(return_value=Response(200, json=events))

    response = post_chat("read x")

    assert response.status_code == 200
    assert "Thinking Process" in response.text
    assert "read_file" in response.text
    assert "done" in response.text


@respx.mock
def test_chat_reports_agent_error():
    respx.get(f"{AGENT_URL}/apps/dak_agent/users/{USER_ID}/sessions/{SESSION_ID}").mock(
        return_value=Response(200, json={"id": SESSION_ID})
    )
    respx.post(f"{AGENT_URL}/run").mock(return_value=Response(500, text="boom"))

    response = post_chat()

    assert response.status_code == 200  # errors are rendered into the stream
    assert 'class="chat-message error"' in response.text


def confirmation_event(fc_id: str, tool: str, args: dict) -> dict:
    return {"content": {"parts": [{"functionCall": {
        "id": fc_id, "name": "adk_request_confirmation",
        "args": {"originalFunctionCall": {"name": tool, "args": args}}}}]}}


@respx.mock
def test_chat_renders_confirmation_as_escaped_approval_card():
    respx.get(f"{AGENT_URL}/apps/dak_agent/users/{USER_ID}/sessions/{SESSION_ID}").mock(
        return_value=Response(200, json={"id": SESSION_ID})
    )
    args = {"content": "<script>x</script>"}
    events = [
        # The held call itself comes first, as a thought; its arguments are the model's too
        {"content": {"parts": [{"functionCall": {"name": "write_file", "args": args}}]}},
        {"content": {"parts": [{"functionResponse": {"name": "write_file", "response": {"error": "<script>y</script>"}}}]}},
        adk_text_event("<script>z</script>"),
        confirmation_event("fc-1", "write_file", args),
        adk_text_event("after"),
    ]
    respx.post(f"{AGENT_URL}/run").mock(return_value=Response(200, json=events))

    response = post_chat("write")

    assert 'hx-post="/chat/approvals/fc-1"' in response.text
    assert f'name="session_id" value="{SESSION_ID}"' in response.text
    assert "<script>" not in response.text  # nothing the model wrote runs next to the Approve button
    assert "after" not in response.text  # the turn ends at the confirmation


@respx.mock
def test_approval_answer_goes_to_agent_reply_route():
    reply = respx.post(f"{AGENT_URL}/approvals/fc-1/reply").mock(
        return_value=Response(200, json=[adk_text_event("resumed")])
    )

    response = client.post("/chat/approvals/fc-1", data={"mode": "reject", "session_id": SESSION_ID, "user_id": USER_ID})

    assert response.status_code == 200
    assert "resumed" in response.text
    assert json.loads(reply.calls.last.request.content) == {"user_id": USER_ID, "session_id": SESSION_ID, "mode": "reject"}


def post_answer(mode: str = "once"):
    return client.post("/chat/approvals/fc-1", data={"mode": mode, "session_id": SESSION_ID, "user_id": USER_ID})


@respx.mock
def test_expired_approval_says_the_agent_moved_on():
    respx.post(f"{AGENT_URL}/approvals/fc-1/reply").mock(
        return_value=Response(409, json={"observation": "timed_out"})
    )

    response = post_answer()

    assert response.status_code == 200
    assert 'class="chat-message system"' in response.text
    assert "expired" in response.text and "timed out" in response.text


@respx.mock
def test_answered_approval_says_it_is_no_longer_pending():
    respx.post(f"{AGENT_URL}/approvals/fc-1/reply").mock(return_value=Response(404, json={"detail": "fc-1 is not pending"}))

    response = post_answer()

    assert response.status_code == 200
    assert 'class="chat-message system"' in response.text
    assert "no longer pending" in response.text


@respx.mock
def test_unreachable_agent_is_shown_as_error():
    respx.post(f"{AGENT_URL}/approvals/fc-1/reply").mock(side_effect=httpx.ConnectError("refused"))

    response = post_answer()

    assert response.status_code == 200
    assert 'class="chat-message error"' in response.text
