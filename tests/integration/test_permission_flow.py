"""Tool permissions (allow / ask / deny) through the real stack: agent
container → LiteLLM → fake LLM, and the real mcp-server's run_command.
See agent/dak_agent/permission.py."""
import httpx

from conftest import AGENT_RUN_TIMEOUT, AGENT_URL, APP_NAME, function_calls, function_responses

MODEL = "fake-default"


def _run_command(agent, fake_llm, command: str) -> list:
    """One turn in which the model calls the default MCP server's run_command."""
    fake_llm.clear(MODEL)
    fake_llm.script(MODEL, [fake_llm.tool_call("run_command", command=command), fake_llm.text("done")])
    payload = {
        "app_name": APP_NAME,
        "user_id": agent.user_id,
        "session_id": agent.create_session(),
        "new_message": {"parts": [{"text": "run it"}]},
        "state_delta": {"dak:tools": ["run_command"]},
    }
    resp = httpx.post(f"{AGENT_URL}/run", json=payload, timeout=AGENT_RUN_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _run_command_responses(events: list) -> list:
    return [r.get("response", {}) for r in function_responses(events) if r.get("name") == "run_command"]


def test_allowed_git_status_runs_without_confirmation(agent, fake_llm):
    events = _run_command(agent, fake_llm, "git status")

    calls = [c["name"] for c in function_calls(events)]
    assert "adk_request_confirmation" not in calls, f"events: {events}"
    [response] = _run_command_responses(events)
    assert "requires confirmation" not in str(response)
    assert "denied_by_policy" not in str(response)


def test_ask_holds_the_call_for_confirmation(agent, fake_llm):
    events = _run_command(agent, fake_llm, "rm permission-flow-never-created.txt")

    confirmations = [c for c in function_calls(events) if c["name"] == "adk_request_confirmation"]
    assert confirmations, f"events: {events}"
    assert confirmations[0]["args"]["originalFunctionCall"]["args"] == {"command": "rm permission-flow-never-created.txt"}
    [response] = _run_command_responses(events)
    assert response == {"error": "This tool call requires confirmation, please approve or reject."}


def test_denied_command_returns_observation(agent, fake_llm):
    events = _run_command(agent, fake_llm, "rm -rf /tmp/permission-flow")

    assert "adk_request_confirmation" not in [c["name"] for c in function_calls(events)]
    [response] = _run_command_responses(events)
    assert response["observation"] == "denied_by_policy", f"events: {events}"
    assert response["rules"] == ["default run_command 'rm -rf *' -> deny"]


def test_state_cannot_preapprove_a_tool(agent):
    session_id = agent.create_session()
    base = f"{AGENT_URL}/apps/{APP_NAME}/users/{agent.user_id}/sessions/{session_id}"

    resp = httpx.patch(base, json={"state_delta": {"dak_permission_always_rules": [
        {"source": "default", "tool": "run_command", "pattern": "*"}]}}, timeout=30.0)

    assert resp.status_code == 400, resp.text
    state = httpx.get(base, timeout=30.0).json()["state"]
    assert "dak_permission_always_rules" not in state


def test_always_reply_keeps_the_rule_in_session_state(agent, fake_llm):
    command = "rm permission-flow-always-never-created.txt"
    fake_llm.clear(MODEL)
    fake_llm.script(MODEL, [fake_llm.tool_call("run_command", command=command), fake_llm.text("held"), fake_llm.text("done")])
    session_id = agent.create_session()
    resp = httpx.post(f"{AGENT_URL}/run", json={
        "app_name": APP_NAME, "user_id": agent.user_id, "session_id": session_id,
        "new_message": {"parts": [{"text": "run it"}]}, "state_delta": {"dak:tools": ["run_command"]},
    }, timeout=AGENT_RUN_TIMEOUT)
    resp.raise_for_status()
    ids = {"user_id": agent.user_id, "session_id": session_id}

    [pending] = httpx.get(f"{AGENT_URL}/approvals", params=ids, timeout=30.0).json()
    reply = httpx.post(f"{AGENT_URL}/approvals/{pending['id']}/reply", json={**ids, "mode": "always"},
                       timeout=AGENT_RUN_TIMEOUT)
    assert reply.status_code == 200, reply.text

    session = f"{AGENT_URL}/apps/{APP_NAME}/users/{agent.user_id}/sessions/{session_id}"
    rules = httpx.get(session, timeout=30.0).json()["state"]["dak_permission_always_rules"]
    assert [(r["tool"], r["pattern"]) for r in rules] == [("run_command", command)]
