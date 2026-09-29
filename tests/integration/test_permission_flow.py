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
