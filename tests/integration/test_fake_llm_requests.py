"""The fake LLM records what the agent sent it (messages and tool definitions),
per model, so tests can inspect the requests and clear them between runs."""
import httpx

from conftest import FAKE_LLM_URL, event_texts, system_instruction, tool_names

MODEL = "fake-requests-log"
TOOLS = [{"type": "function", "function": {"name": "dummy_tool", "parameters": {}}}]


def _send(**extra):
    resp = httpx.post(f"{FAKE_LLM_URL}/v1/chat/completions", json={
        "model": f"openai/{MODEL}",
        "messages": [{"role": "user", "content": "hello"}],
        **extra,
    }, timeout=10.0)
    resp.raise_for_status()


def _recorded() -> list:
    return httpx.get(f"{FAKE_LLM_URL}/requests/{MODEL}", timeout=10.0).json()


def test_get_requests_returns_tools_and_messages(fake_llm):
    fake_llm.clear(MODEL)
    _send(tools=TOOLS)
    _send()

    with_tools, without_tools = _recorded()
    assert with_tools["messages"] == [{"role": "user", "content": "hello"}]
    assert with_tools["tools"][0]["function"]["name"] == "dummy_tool"
    assert without_tools["tools"] == []


def test_delete_script_clears_requests_log(fake_llm):
    _send(tools=TOOLS)
    assert _recorded()

    fake_llm.clear(MODEL)
    assert _recorded() == []


def test_system_instruction_and_tool_names_helpers():
    entry = {
        "messages": [
            {"role": "system", "content": "You are DAK."},
            {"role": "user", "content": "hello"},
            {"role": "system", "content": "not the first"},
        ],
        "tools": [{"type": "function", "function": {"name": n, "parameters": {}}} for n in ("b_tool", "a_tool")],
    }
    assert system_instruction(entry) == "You are DAK."
    assert tool_names(entry) == ["b_tool", "a_tool"]  # in the order sent, not sorted

    assert system_instruction({"messages": [{"role": "user", "content": "hello"}], "tools": []}) == ""
    assert tool_names({"messages": [], "tools": []}) == []


def test_fake_llm_requests_returns_the_log(fake_llm):
    fake_llm.clear(MODEL)
    _send(tools=TOOLS)
    assert fake_llm.requests(MODEL) == _recorded()
    assert tool_names(fake_llm.requests(MODEL)[-1]) == ["dummy_tool"]


# --- Anthropic /v1/messages and the thinking-resend rule (#765) ---

CLAUDE = "fake-thinking-rule"
SIGNED = {"role": "assistant", "content": [
    {"type": "thinking", "thinking": "hmm", "signature": "sig-1"},
    {"type": "text", "text": "hi"},
]}


def _messages(system: str, messages: list) -> httpx.Response:
    return httpx.post(f"{FAKE_LLM_URL}/v1/messages", json={
        "model": f"anthropic/{CLAUDE}", "max_tokens": 16, "system": system, "messages": messages,
    }, timeout=10.0)


def test_thinking_resend_after_change_is_400(fake_llm):
    fake_llm.clear(CLAUDE)
    first = [{"role": "user", "content": "hello"}]
    assert _messages("sys A", first).status_code == 200
    resp = _messages("sys B", first + [SIGNED, {"role": "user", "content": "more"}])
    assert resp.status_code == 400
    assert resp.json()["error"]["type"] == "invalid_request_error"


def test_thinking_resend_without_change_is_200(fake_llm):
    fake_llm.clear(CLAUDE)
    first = [{"role": "user", "content": "hello"}]
    assert _messages("sys A", first).status_code == 200
    assert _messages("sys A", first + [SIGNED, {"role": "user", "content": "more"}]).status_code == 200


def test_change_without_thinking_is_200(fake_llm):
    fake_llm.clear(CLAUDE)
    first = [{"role": "user", "content": "hello"}]
    assert _messages("sys A", first).status_code == 200
    plain = {"role": "assistant", "content": [{"type": "text", "text": "hi"}]}
    assert _messages("sys B", first + [plain, {"role": "user", "content": "more"}]).status_code == 200


def test_scripted_thinking_is_returned_signed(fake_llm):
    fake_llm.clear(CLAUDE)
    fake_llm.script(CLAUDE, [fake_llm.thinking("pondering", fake_llm.text("done"))])
    body = _messages("sys A", [{"role": "user", "content": "hello"}]).json()
    assert [b["type"] for b in body["content"]] == ["thinking", "text"]
    assert body["content"][0]["signature"].startswith("sig-")
    assert body["stop_reason"] == "end_turn"


def test_anthropic_route_reaches_messages_endpoint(agent, fake_llm):
    fake_llm.clear("fake-claude")
    fake_llm.script("fake-claude", [fake_llm.text("claude says hi")])
    events = agent.run(agent.create_session({"dak:model": "anthropic/fake-claude"}), "hello")
    assert "claude says hi" in event_texts(events)
    assert len(fake_llm.requests("fake-claude")) == 1
