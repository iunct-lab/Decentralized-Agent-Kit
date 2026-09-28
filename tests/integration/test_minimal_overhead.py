"""The minimal DAK call costs what a direct LLM call costs (PBI #139).

A call that sets only an instruction and an output schema, and selects no
tools (`dak:tools: []`), must reach the LLM once, with no tool definitions,
and with a prompt within OVERHEAD_TOKENS of the same call made directly.
The fake LLM's `usage` is a constant, so tokens are estimated from the
messages it recorded, with the same heuristic as the agent's harness.
"""
import json

import httpx

from conftest import AGENT_RUN_TIMEOUT, AGENT_URL, APP_NAME, FAKE_LLM_URL, event_texts

MODEL = "fake-default"
INSTRUCTION = "Reply with a JSON object that says whether the user's sentence is a greeting."
PROMPT = "Good morning, everyone."
# The caller's instruction replaces DAK's base instruction, so the only
# DAK-specific text left is the identity line ADK appends to every agent
# ("You are an agent. Your internal name is ...", about 13 tokens). 20 leaves
# a small margin for that line; anything larger means DAK added something.
OVERHEAD_TOKENS = 20


def estimate_tokens(text: str) -> int:
    """Same estimate as `agent/dak_agent/harness.py::estimate_tokens`; the
    integration tests do not install the agent package."""
    if not text:
        return 0
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    return ascii_chars // 4 + (len(text) - ascii_chars)


def _recorded(model: str) -> list:
    return httpx.get(f"{FAKE_LLM_URL}/requests/{model}", timeout=10.0).json()


def _direct_call_tokens() -> int:
    """The baseline: the same instruction and prompt sent straight to the LLM."""
    resp = httpx.post(f"{FAKE_LLM_URL}/v1/chat/completions", json={
        "model": MODEL,
        "messages": [{"role": "system", "content": INSTRUCTION}, {"role": "user", "content": PROMPT}],
    }, timeout=10.0)
    resp.raise_for_status()
    return estimate_tokens(json.dumps(_recorded(MODEL)[-1]["messages"]))


def test_minimal_call_is_one_llm_request_without_tools_and_no_extra_tokens(agent, fake_llm):
    fake_llm.clear(MODEL)
    baseline_tokens = _direct_call_tokens()
    fake_llm.clear(MODEL)
    fake_llm.script(MODEL, [fake_llm.text('{"ok": true}')])

    resp = httpx.post(f"{AGENT_URL}/run", json={
        "app_name": APP_NAME,
        "user_id": agent.user_id,
        "session_id": agent.create_session(),
        "new_message": {"parts": [{"text": PROMPT}]},
        "state_delta": {"dak:instruction": INSTRUCTION, "dak:output_schema": {"type": "object"}, "dak:tools": []},
    }, timeout=AGENT_RUN_TIMEOUT)
    resp.raise_for_status()
    assert json.loads(event_texts(resp.json())[-1]) == {"ok": True}

    # One request in total. The Meta-LLM (mode switching, skills) calls the
    # same model name, so this also shows it was not called.
    requests = _recorded(MODEL)
    assert len(requests) == 1, requests
    assert not requests[0]["tools"], requests[0]["tools"]
    dak_tokens = estimate_tokens(json.dumps(requests[0]["messages"]))
    assert abs(dak_tokens - baseline_tokens) <= OVERHEAD_TOKENS, (dak_tokens, baseline_tokens, requests[0]["messages"])
