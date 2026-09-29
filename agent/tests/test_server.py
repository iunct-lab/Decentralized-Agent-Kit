"""The HTTP entry point: ADK's routes stay, `/approvals` is added on top."""
import os
import time

from unittest.mock import patch

import pytest

os.environ.setdefault("SESSION_SERVICE_URI", "memory://")

from fastapi.testclient import TestClient  # noqa: E402

from dak_agent.server import app  # noqa: E402

CONFIRMATION = {
    "author": "dak_agent",
    "content": {"role": "model", "parts": [{"functionCall": {
        "id": "adk-1",
        "name": "adk_request_confirmation",
        "args": {
            "originalFunctionCall": {"id": "call-1", "name": "planner", "args": {"task_description": "t"}},
            "toolConfirmation": {"hint": "Please approve", "confirmed": False},
        },
    }}]},
}


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def history():
    """The session's events as ADK's session route would return them. ADK
    refuses to create a session that already holds `adk_request_confirmation`,
    so the read is replaced; the real round trip is in tests/integration."""
    events = []

    async def read(client, app_name, user_id, session_id):
        return events

    with patch("dak_agent.server._session_events", read):
        yield events


def _user_text(text):
    return {"author": "user", "content": {"role": "user", "parts": [{"text": text}]}}


def test_approval_routes_are_added_next_to_adk_routes(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert {"/approvals", "/approvals/{approval_id}/reply", "/approvals/stream", "/run"} <= paths.keys()
    assert client.get("/list-apps").status_code == 200


def test_list_shows_the_pending_approval(client, history):
    history += [_user_text("plan"), {**CONFIRMATION, "timestamp": time.time()}]
    sid = "s1"
    [item] = client.get("/approvals", params={"user_id": "u", "session_id": sid}).json()
    assert (item["id"], item["kind"], item["tool_name"], item["status"], item["session_id"]) == \
        ("adk-1", "approval", "planner", "pending", sid)


def test_list_marks_an_expired_approval_without_consuming_it(client, history):
    history += [_user_text("plan"), {**CONFIRMATION, "timestamp": time.time() - 10_000}]
    sid = "s1"
    for _ in range(2):
        [item] = client.get("/approvals", params={"user_id": "u", "session_id": sid}).json()
        assert item["status"] == "timed_out"


def test_reply_to_an_id_that_is_not_pending_is_404(client, history):
    history += [_user_text("plan"), {**CONFIRMATION, "timestamp": time.time()}]
    sid = "s1"
    resp = client.post("/approvals/adk-unknown/reply", json={"user_id": "u", "session_id": sid, "mode": "once"})
    assert resp.status_code == 404


def test_reply_with_an_unknown_mode_is_422(client, history):
    history += [_user_text("plan"), {**CONFIRMATION, "timestamp": time.time()}]
    sid = "s1"
    resp = client.post("/approvals/adk-1/reply", json={"user_id": "u", "session_id": sid, "mode": "approve"})
    assert resp.status_code == 422


def test_reply_to_an_approval_needs_a_mode(client, history):
    history += [_user_text("plan"), {**CONFIRMATION, "timestamp": time.time()}]
    sid = "s1"
    resp = client.post("/approvals/adk-1/reply", json={"user_id": "u", "session_id": sid, "answer": "yes"})
    assert resp.status_code == 422


def test_list_of_an_unknown_session_is_404(client):
    assert client.get("/approvals", params={"user_id": "u", "session_id": "nope"}).status_code == 404


def test_stream_of_an_unknown_session_is_404(client):
    assert client.get("/approvals/stream", params={"user_id": "u", "session_id": "nope"}).status_code == 404
