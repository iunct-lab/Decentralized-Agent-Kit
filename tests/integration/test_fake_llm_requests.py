"""The fake LLM records what the agent sent it (messages and tool definitions),
per model, so tests can inspect the requests and clear them between runs."""
import httpx

from conftest import FAKE_LLM_URL, system_instruction, tool_names

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
