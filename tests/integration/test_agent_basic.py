"""Basic agent E2E: session creation and a scripted text exchange."""
from conftest import event_texts

MODEL = "fake-default"


def test_session_create_and_text_response(agent, fake_llm):
    fake_llm.clear(MODEL)
    fake_llm.script(MODEL, [fake_llm.text("Hello! I am the DAK agent.")])

    session_id = agent.create_session()
    events = agent.run(session_id, "Hello, who are you?")

    texts = event_texts(events)
    assert any("Hello! I am the DAK agent." in t for t in texts), f"events: {events}"


def test_conversation_history_persists(agent, fake_llm):
    fake_llm.clear(MODEL)
    fake_llm.script(MODEL, [fake_llm.text("first answer"), fake_llm.text("second answer")])

    session_id = agent.create_session()
    agent.run(session_id, "first message")
    events = agent.run(session_id, "second message")

    assert any("second answer" in t for t in event_texts(events))

    # The session transcript should contain both turns
    import httpx
    from conftest import APP_NAME

    resp = httpx.get(
        f"{agent.base_url}/apps/{APP_NAME}/users/{agent.user_id}/sessions/{session_id}",
        timeout=30.0,
    )
    resp.raise_for_status()
    transcript = str(resp.json())
    assert "first message" in transcript
    assert "second message" in transcript


def test_two_sessions_call_mcp_tools_independently_with_session_headers(fake_llm):
    """Two users' sessions call the same MCP tool through the same cached
    toolset; each call carries its own session key (`X-DAK-Session-Key`) and
    so its own pooled MCP connection. Both must complete, each with its own
    result, and neither transcript may contain the other's call."""
    import uuid

    import httpx
    from conftest import AGENT_RUN_TIMEOUT, AGENT_URL, APP_NAME, AgentClient, function_calls, function_responses

    def run_with_mcp_tool(client, session_id, prompt):
        # A session starts with built-in tools only; `dak:tools` adds the
        # default MCP server's `deep_think` for this call.
        payload = {
            "app_name": APP_NAME,
            "user_id": client.user_id,
            "session_id": session_id,
            "new_message": {"parts": [{"text": prompt}]},
            "state_delta": {"dak:tools": ["deep_think"]},
        }
        resp = httpx.post(f"{AGENT_URL}/run", json=payload, timeout=AGENT_RUN_TIMEOUT)
        resp.raise_for_status()
        return resp.json()

    def transcript(client, session_id):
        resp = httpx.get(f"{AGENT_URL}/apps/{APP_NAME}/users/{client.user_id}/sessions/{session_id}", timeout=30.0)
        resp.raise_for_status()
        return str(resp.json())

    client_a = AgentClient(AGENT_URL, user_id=f"it_user_a_{uuid.uuid4().hex[:8]}")
    client_b = AgentClient(AGENT_URL, user_id=f"it_user_b_{uuid.uuid4().hex[:8]}")
    session_a_id = client_a.create_session()
    session_b_id = client_b.create_session()

    fake_llm.clear(MODEL)
    # One FIFO queue per model: A's two responses, then B's.
    fake_llm.script(MODEL, [
        fake_llm.tool_call("deep_think", thought="session A"),
        fake_llm.text("A done"),
        fake_llm.tool_call("deep_think", thought="session B"),
        fake_llm.text("B done"),
    ])

    events_a = run_with_mcp_tool(client_a, session_a_id, "think about A")
    events_b = run_with_mcp_tool(client_b, session_b_id, "think about B")

    for events, thought, done in ((events_a, "session A", "A done"), (events_b, "session B", "B done")):
        assert "deep_think" in [c["name"] for c in function_calls(events)], f"events: {events}"
        result = next(r for r in function_responses(events) if r.get("name") == "deep_think")
        # The mcp-server's `deep_think` echoes the thought: the call reached it.
        assert thought in str(result.get("response", {})), f"events: {events}"
        assert any(done in t for t in event_texts(events)), f"events: {events}"

    history_a = transcript(client_a, session_a_id)
    history_b = transcript(client_b, session_b_id)
    assert "session A" in history_a and "session B" not in history_a
    assert "session B" in history_b and "session A" not in history_b
