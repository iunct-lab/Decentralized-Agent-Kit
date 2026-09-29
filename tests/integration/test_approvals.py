"""Pending approvals and questions answered through the agent's /approvals
routes (PBI #100, docs/design/approval-queue.md).

The approval is started from the BFF, the way a user would, with `planner`
(DAK_PLANNER_REQUIRE_CONFIRMATION in docker-compose.test.yml): an agent-side
tool, so the scripted turn does not depend on the MCP server. MCP tools ask
through PermissionPlugin in the same `adk_request_confirmation` shape.
"""
import json
import os
import re
import subprocess
import time
import uuid

import httpx

from conftest import AGENT_ENFORCER_URL, AGENT_RUN_TIMEOUT, AGENT_URL, BFF_URL, FAKE_LLM_URL, function_responses

MODEL = "fake-default"
ENFORCER_MODEL = "fake-enforcer"
TIMEOUT_SECONDS = 5  # DAK_APPROVAL_TIMEOUT_SECONDS in docker-compose.test.yml


def _plan_call(fake_llm):
    return fake_llm.tool_call("planner", task_description="tidy", plan_steps=["tidy"], allowed_tools=[])


def _start_from_bff(fake_llm, *after):
    """Ask through the BFF; the scripted model calls `planner`, which waits for approval."""
    fake_llm.clear(MODEL)
    fake_llm.script(MODEL, [_plan_call(fake_llm), *after])
    session_id = f"session_bff_it_{uuid.uuid4().hex[:8]}"
    user_id = f"user_{session_id}"
    resp = httpx.post(f"{BFF_URL}/chat", data={"prompt": "Tidy up", "session_id": session_id, "user_id": user_id},
                      timeout=AGENT_RUN_TIMEOUT)
    resp.raise_for_status()
    # The agent names a new session itself; the BFF hands the id back to the page (HTMX out-of-band swap)
    swapped = re.search(r'id="session-id-input" name="session_id" value="([^"]+)"', resp.text)
    return user_id, swapped.group(1) if swapped else session_id


def _pending(user_id, session_id, base=AGENT_URL):
    resp = httpx.get(f"{base}/approvals", params={"user_id": user_id, "session_id": session_id}, timeout=30.0)
    resp.raise_for_status()
    return resp.json()


def _reply(approval_id, user_id, session_id, base=AGENT_URL, **body):
    return httpx.post(f"{base}/approvals/{approval_id}/reply",
                      json={"user_id": user_id, "session_id": session_id, **body}, timeout=AGENT_RUN_TIMEOUT)


def _session_events(user_id, session_id, base=AGENT_URL):
    resp = httpx.get(f"{base}/apps/dak_agent/users/{user_id}/sessions/{session_id}", timeout=30.0)
    resp.raise_for_status()
    return resp.json()["events"]


def _last_model_request(model):
    return json.dumps(httpx.get(f"{FAKE_LLM_URL}/requests/{model}", timeout=10.0).json()[-1]["messages"])


def _answers(events):
    return [r["response"] for r in function_responses(events) if r["name"] == "adk_request_confirmation"]


def test_pending_started_from_bff_is_answered_once(fake_llm):
    user_id, session_id = _start_from_bff(fake_llm, fake_llm.text("Plan approved and done."))

    [item] = _pending(user_id, session_id)
    assert (item["kind"], item["tool_name"], item["status"]) == ("approval", "planner", "pending")

    resp = _reply(item["id"], user_id, session_id, mode="once")
    assert resp.status_code == 200, resp.text
    assert "Plan approved and done." in json.dumps(resp.json())
    assert _pending(user_id, session_id) == []

    # A second client answering the same approval is refused, and planner ran once
    assert _reply(item["id"], user_id, session_id, mode="once").status_code == 404
    events = _session_events(user_id, session_id)
    planner_results = [r for r in function_responses(events)
                       if r["name"] == "planner" and "error" not in r["response"]]
    assert len(planner_results) == 1
    assert _answers(events)[-1]["payload"]["mode"] == "once"


def test_always_is_recorded_apart_from_once(fake_llm):
    user_id, session_id = _start_from_bff(fake_llm, fake_llm.text("Done."))
    [item] = _pending(user_id, session_id)

    assert _reply(item["id"], user_id, session_id, mode="always").status_code == 200
    assert _answers(_session_events(user_id, session_id))[-1] == \
        {"confirmed": True, "payload": {"mode": "always", "reason": ""}}


def test_reject_reason_reaches_model(fake_llm):
    user_id, session_id = _start_from_bff(fake_llm, fake_llm.text("Understood, not planning."))
    [item] = _pending(user_id, session_id)

    resp = _reply(item["id"], user_id, session_id, mode="reject", reason="not now")
    assert resp.status_code == 200, resp.text

    last = _last_model_request(MODEL)
    assert "denied_by_user" in last and "not now" in last
    assert _pending(user_id, session_id) == []


def test_timed_out_pending_returns_observation(fake_llm):
    user_id, session_id = _start_from_bff(fake_llm, fake_llm.text("Too late, moving on."))
    time.sleep(TIMEOUT_SECONDS + 1)

    [item] = _pending(user_id, session_id)
    assert item["status"] == "timed_out"
    assert _pending(user_id, session_id)[0]["status"] == "timed_out"  # listing does not consume it

    resp = _reply(item["id"], user_id, session_id, mode="once")
    assert resp.status_code == 409
    assert resp.json() == {"observation": "timed_out"}
    assert "timed_out" in _last_model_request(MODEL)
    assert _pending(user_id, session_id) == []


def test_question_is_listed_and_answered(fake_llm):
    """ask_question (Enforcer Mode) waits for the next user message; it is
    listed next to approvals and answered through the same route."""
    fake_llm.clear(ENFORCER_MODEL)
    fake_llm.script(ENFORCER_MODEL, [
        fake_llm.tool_call("ask_question", questions=["Which branch?"], context="Two branches match"),
        fake_llm.tool_call("attempt_answer", answer="Deploying main", confidence="high", sources_used=[]),
    ])
    user_id = f"it_user_{uuid.uuid4().hex[:8]}"
    session_id = httpx.post(f"{AGENT_ENFORCER_URL}/apps/dak_agent/users/{user_id}/sessions", json={},
                            timeout=30.0).json()["id"]
    httpx.post(f"{AGENT_ENFORCER_URL}/run", timeout=AGENT_RUN_TIMEOUT, json={
        "app_name": "dak_agent", "user_id": user_id, "session_id": session_id,
        "new_message": {"parts": [{"text": "Deploy it"}]}}).raise_for_status()

    [item] = _pending(user_id, session_id, AGENT_ENFORCER_URL)
    assert (item["kind"], item["questions"], item["context"]) == ("question", ["Which branch?"], "Two branches match")

    resp = _reply(item["id"], user_id, session_id, AGENT_ENFORCER_URL, answer="the main branch")
    assert resp.status_code == 200, resp.text
    assert "the main branch" in _last_model_request(ENFORCER_MODEL)
    assert _pending(user_id, session_id, AGENT_ENFORCER_URL) == []


def test_stream_announces_asked_and_replied(fake_llm):
    user_id, session_id = _start_from_bff(fake_llm, fake_llm.text("Done."))
    params = {"user_id": user_id, "session_id": session_id}

    with httpx.stream("GET", f"{AGENT_URL}/approvals/stream", params=params, timeout=30.0) as stream:
        lines = stream.iter_lines()
        assert next(lines) == "event: approval.asked"
        asked = json.loads(next(lines).removeprefix("data: "))
        assert asked["tool_name"] == "planner"

        assert _reply(asked["id"], user_id, session_id, mode="once").status_code == 200
        for line in lines:
            if line.startswith("event: "):
                assert line == "event: approval.replied"
                assert json.loads(next(lines).removeprefix("data: ")) == {"id": asked["id"]}
                break


CLI_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "cli"))


def _dak_cli(tmp_path, *args):
    """dak-cli with its config (~/.dak-cli) isolated, as test_cli.py does. Logged in as someone else
    than the BFF's user, so the session is reached only through --user."""
    env = {**os.environ, "HOME": str(tmp_path), "DAK_AGENT_URL": AGENT_URL}
    config_dir = tmp_path / ".dak-cli"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "config.json").write_text(json.dumps({"username": "it_cli_user"}))
    return subprocess.run(["uv", "run", "dak-cli", *args], cwd=CLI_DIR, env=env,
                          capture_output=True, text=True, timeout=300)


def test_bff_pending_answered_by_dak_cli(fake_llm, tmp_path):
    """Acceptance 1 from the CLI side: a pending approval the BFF started is listed and answered by dak-cli,
    with the same result as any other client, and a second answer is refused."""
    user_id, session_id = _start_from_bff(fake_llm, fake_llm.text("Answered from the CLI."))
    [item] = _pending(user_id, session_id)

    listed = _dak_cli(tmp_path, "approvals", "--session", session_id, "--user", user_id)
    assert listed.returncode == 0, listed.stdout + listed.stderr
    assert item["id"] in listed.stdout and "planner" in listed.stdout

    answered = _dak_cli(tmp_path, "approve", item["id"], "--session", session_id, "--user", user_id)
    assert answered.returncode == 0, answered.stdout + answered.stderr
    assert "Answered from the CLI." in answered.stdout
    assert _pending(user_id, session_id) == []
    events = _session_events(user_id, session_id)
    assert _answers(events)[-1]["payload"]["mode"] == "once"
    assert "Answered from the CLI." in json.dumps(events)

    again = _dak_cli(tmp_path, "approve", item["id"], "--session", session_id, "--user", user_id)
    assert again.returncode != 0
    assert "404" in again.stdout
