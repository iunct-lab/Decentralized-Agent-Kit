"""The `request` line: one per request, only the fields it names (docs/architecture/overview.md)."""
import os
import re
from unittest.mock import patch

import pytest

os.environ.setdefault("SESSION_SERVICE_URI", "memory://")

from fastapi.testclient import TestClient  # noqa: E402
from opentelemetry import trace  # noqa: E402

with patch("dak_agent.agent_card.add_skills_in_place", return_value=0):
    from dak_agent import request_log, server  # noqa: E402


@pytest.fixture
def lines(caplog):
    request_log.logger.addHandler(caplog.handler)  # the logger does not propagate to caplog's root handler
    try:
        with TestClient(server.app) as client:
            yield client, lambda: [r.getMessage() for r in caplog.records if r.name == request_log.logger.name]
    finally:
        request_log.logger.removeHandler(caplog.handler)


def run_body(**extra):
    return {"app_name": "dak_agent", "user_id": "u1", "session_id": "s1",
            "new_message": {"role": "user", "parts": [{"text": "SECRET-PROMPT-730"}]}, **extra}


def test_run_logs_one_line_without_prompt(lines):
    client, logged = lines
    client.post("/run", json=run_body())
    [line] = logged()
    assert line.startswith("request ")
    for part in ("method=POST", 'path="/run"', "status=404", 'user_id="u1"', 'session_id="s1"'):
        assert part in line
    assert "SECRET-PROMPT-730" not in line


def test_session_route_ids_from_path(lines):
    client, logged = lines
    client.post("/apps/dak_agent/users/u2/sessions", json={})
    [line] = logged()
    assert 'user_id="u2"' in line and 'app_name="dak_agent"' in line and "status=200" in line


def test_reply_logs_approval_id_and_mode(lines):
    client, logged = lines
    client.post("/approvals/adk-1/reply", json={
        "user_id": "u1", "session_id": "s1", "mode": "reject", "reason": "SECRET-REASON"})
    outer = [l for l in logged() if "client=in-process" not in l]
    inner = [l for l in logged() if "client=in-process" in l]
    assert len(outer) == 1 and inner
    assert 'approval_id="adk-1"' in outer[0] and "mode=reject" in outer[0] and "status=404" in outer[0]
    assert not any("SECRET-REASON" in l for l in logged())


def test_question_answer_not_logged(lines):
    client, logged = lines
    client.post("/approvals/q-1/reply", json={"user_id": "u1", "session_id": "s1", "answer": "SECRET-ANSWER"})
    client.post("/approvals/q-1/reply", json={"user_id": "u1", "session_id": "s1", "mode": "bogus"})
    outer = [l for l in logged() if "client=in-process" not in l]
    assert len(outer) == 2 and all("mode=-" in l for l in outer)
    assert not any("SECRET-ANSWER" in l for l in logged())


def test_trace_id_is_the_handlers_trace(lines):
    client, logged = lines
    seen = {}

    async def events(*_):
        seen["trace_id"] = format(trace.get_current_span().get_span_context().trace_id, "032x")
        return []

    with patch.object(server, "_session_events", events):
        client.get("/approvals?user_id=u&session_id=s")
    [line] = [l for l in logged() if 'path="/approvals"' in l]
    match = re.search(r"trace_id=([0-9a-f]{32})", line)
    assert match and match.group(1) == seen["trace_id"] != "0" * 32


def test_forged_line_is_escaped(lines):
    client, logged = lines
    client.post("/run", json=run_body(user_id="u\nrequest client=evil"))
    [line] = logged()
    assert "\n" not in line and "\\n" in line


def test_fields_from_query_and_body():
    fields = request_log.request_fields(
        "/run", {"user_id": "q"}, {"session_id": "b", "app_name": 5, "state_delta": {"k": "v"}})
    assert fields == {"user_id": "q", "session_id": "b"}


def test_body_ids_win_over_query_and_odd_mode_is_ignored():
    fields = request_log.request_fields(
        "/approvals/a/reply", {"user_id": "decoy", "session_id": "decoy"},
        {"user_id": "victim", "session_id": "real", "mode": []})
    assert fields == {"user_id": "victim", "session_id": "real", "approval_id": "a"}


def test_handler_adds_a_timestamp():
    [handler] = [h for h in request_log.logger.handlers if type(h) is request_log.logging.StreamHandler]
    record = request_log.logging.LogRecord("n", 20, "f", 1, "request x", None, None)
    assert re.match(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} request x$", handler.format(record))
