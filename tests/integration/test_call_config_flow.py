"""Per-call settings (`dak:` keys in `/run`'s `state_delta`) through the real
stack: agent container → LiteLLM → fake LLM. See docs/architecture/call_config.md."""
import json

import httpx

from conftest import (AGENT_RUN_TIMEOUT, AGENT_URL, APP_NAME, FAKE_LLM_URL, event_texts, function_calls,
                      function_responses)

MODEL = "fake-default"
ALT_MODEL = "fake-alt"  # allowed by DAK_ALLOWED_MODELS in docker-compose.test.yml
DATE_SCHEMA = {"type": "object", "required": ["date"], "properties": {"date": {"type": "string"}}}


def _run(agent, session_id: str, prompt: str, state_delta: dict) -> list:
    """`AgentClient.run` cannot pass `state_delta`; post the payload directly."""
    payload = {
        "app_name": APP_NAME,
        "user_id": agent.user_id,
        "session_id": session_id,
        "new_message": {"parts": [{"text": prompt}]},
        "state_delta": state_delta,
    }
    resp = httpx.post(f"{AGENT_URL}/run", json=payload, timeout=AGENT_RUN_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def test_call_instruction_is_the_system_prompt_the_llm_receives(agent, fake_llm):
    fake_llm.clear(MODEL)
    fake_llm.script(MODEL, [fake_llm.text("ok")])

    _run(agent, agent.create_session(), "hi", {"dak:instruction": "Answer in one word."})

    messages = httpx.get(f"{FAKE_LLM_URL}/requests/{MODEL}", timeout=10.0).json()[-1]["messages"]
    system = messages[0]
    assert system["role"] == "system"
    content = system["content"] if isinstance(system["content"], str) else "".join(
        c.get("text", "") for c in system["content"])
    # The whole system prompt: the caller's instruction plus the identity line
    # ADK appends to every agent's instruction.
    assert content == 'Answer in one word.\n\nYou are an agent. Your internal name is "dak_agent".'


def test_reply_not_matching_output_schema_returns_structured_failure(agent, fake_llm):
    fake_llm.clear(MODEL)
    # A failing reply is regenerated (#140): 3 attempts by default, all invalid.
    fake_llm.script(MODEL, [fake_llm.text('{"note": "missing date"}')] * 3)

    events = _run(agent, agent.create_session(), "when?", {"dak:output_schema": DATE_SCHEMA})

    failures = [json.loads(t) for t in event_texts(events) if "output_schema_validation_failed" in t]
    assert failures, f"events: {events}"
    assert failures[-1]["error"] == "output_schema_validation_failed"
    assert [i["path"] for i in failures[-1]["issues"]] == ["date"]
    assert failures[-1]["attempts"] == _llm_requests(MODEL) == 3


def test_reply_matching_output_schema_is_returned_as_json(agent, fake_llm):
    fake_llm.clear(MODEL)
    fake_llm.script(MODEL, [fake_llm.text('{"date": "2026-09-22"}')])

    events = _run(agent, agent.create_session(), "when?", {"dak:output_schema": DATE_SCHEMA})

    texts = event_texts(events)
    assert json.loads(texts[-1]) == {"date": "2026-09-22"}, f"events: {events}"


def _llm_requests(model: str) -> int:
    return len(httpx.get(f"{FAKE_LLM_URL}/requests/{model}", timeout=10.0).json())


def test_call_model_routes_to_requested_model_only(agent, fake_llm):
    fake_llm.clear(MODEL)
    fake_llm.clear(ALT_MODEL)
    fake_llm.script(ALT_MODEL, [fake_llm.text("from alt model")])

    events = _run(agent, agent.create_session(), "hi", {"dak:model": f"openai/{ALT_MODEL}"})

    assert any("from alt model" in t for t in event_texts(events)), f"events: {events}"
    assert _llm_requests(ALT_MODEL) == 1
    assert _llm_requests(MODEL) == 0


def test_call_model_rejected_when_not_in_allow_list(agent, fake_llm):
    fake_llm.clear(MODEL)
    fake_llm.clear(ALT_MODEL)
    before = {m: _llm_requests(m) for m in (MODEL, ALT_MODEL, "not-allowed")}

    events = _run(agent, agent.create_session(), "hi", {"dak:model": "openai/not-allowed"})

    errors = [json.loads(t) for t in event_texts(events) if "model_not_allowed" in t]
    assert errors, f"events: {events}"
    assert errors[-1]["requested_model"] == "openai/not-allowed"
    assert errors[-1]["allowed_models"] == ["openai/fake-alt", "openai/fake-default"]
    # No LLM was called at all.
    assert {m: _llm_requests(m) for m in before} == before


# Inside the compose network; allowed by DAK_ALLOWED_MCP_URLS in docker-compose.test.yml.
CALLER_MCP = "http://mcp-server:8000/mcp"


def test_caller_mcp_tool_runs_without_a_confirmation_request(agent, fake_llm):
    fake_llm.clear(MODEL)
    fake_llm.script(MODEL, [fake_llm.tool_call("read_file", path="README.md"), fake_llm.text("read it")])

    events = _run(agent, agent.create_session(), "read the README",
                  {"dak:tools": {"mcp_servers": [{"url": CALLER_MCP, "type": "http"}]}})

    calls = [c["name"] for c in function_calls(events)]
    assert "read_file" in calls
    assert "adk_request_confirmation" not in calls
    read = next(r for r in function_responses(events) if r.get("name") == "read_file")
    assert "Decentralized Agent Kit" in str(read.get("response", {}))
    assert any("read it" in t for t in event_texts(events))


def test_caller_mcp_not_in_the_allow_list_is_refused(agent, fake_llm):
    fake_llm.clear(MODEL)
    before = _llm_requests(MODEL)

    events = _run(agent, agent.create_session(), "hi",
                  {"dak:tools": {"mcp_servers": [{"url": "http://not-allowed:9000/mcp"}]}})

    errors = [json.loads(t) for t in event_texts(events) if "mcp_server_not_allowed" in t]
    assert errors and errors[-1]["requested_urls"] == ["http://not-allowed:9000/mcp"]
    assert _llm_requests(MODEL) == before  # no LLM call


def test_unreachable_caller_mcp_ends_the_turn_with_a_reason(agent, fake_llm):
    """The caller's MCP server is down (no such host on the compose network):
    the turn still completes, and the reason is recorded in the session state."""
    fake_llm.clear(MODEL)
    fake_llm.script(MODEL, [fake_llm.text("the tool server is unavailable")])
    url = "http://caller-mcp-unreachable:8000/mcp"  # allowed in docker-compose.test.yml, but no such host

    events = _run(agent, agent.create_session(), "use my tools", {"dak:tools": {"mcp_servers": [{"url": url}]}})

    deltas = [e.get("actions", {}).get("stateDelta") or {} for e in events]
    errors = [d["dak:tools_error"] for d in deltas if d.get("dak:tools_error")]
    assert errors and errors[-1][0]["url"] == url
    assert errors[-1][0]["reason"].startswith("unreachable")
    assert any("the tool server is unavailable" in t for t in event_texts(events))
    system = httpx.get(f"{FAKE_LLM_URL}/requests/{MODEL}", timeout=10.0).json()[-1]["messages"][0]
    content = system["content"] if isinstance(system["content"], str) else "".join(
        c.get("text", "") for c in system["content"])
    assert url in content  # the model was told which tools are unavailable
