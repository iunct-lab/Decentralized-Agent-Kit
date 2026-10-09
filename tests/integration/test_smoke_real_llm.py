"""Real-LLM smoke tests (local Ollama by default).

These verify actual model behavior (prompt quality, tool selection) that the
deterministic fake-LLM suite cannot. A small local model is non-deterministic,
so assertions are deliberately tolerant: they check that the pipeline produced
a sane outcome, not exact wording.

Enable with DAK_SMOKE_REAL_LLM=1 after starting the stack with the
docker-compose.local-llm.yml overlay (see scripts/smoke_local_llm.sh).
"""
import json
import os

import httpx
import pytest

from conftest import AGENT_RUN_TIMEOUT, AGENT_URL, APP_NAME, event_texts, function_calls, function_responses

pytestmark = pytest.mark.skipif(
    os.getenv("DAK_SMOKE_REAL_LLM") != "1",
    reason="real-LLM smoke: start the local-llm stack and set DAK_SMOKE_REAL_LLM=1",
)


def test_basic_chat_responds(agent):
    session_id = agent.create_session()
    events = agent.run(session_id, "Reply with a short greeting in one sentence.")

    texts = event_texts(events)
    assert texts, f"no text in events: {events}"
    assert not any("[ENFORCER_BLOCKED]" in t for t in texts)


def test_skill_discovery_and_use(agent):
    """The model should be able to discover skills and use an MCP tool."""
    session_id = agent.create_session()

    events = agent.run(
        session_id,
        "Use the list_skills tool to show me your available skills. Do not answer from memory.",
    )
    calls = [c["name"] for c in function_calls(events)]
    assert "list_skills" in calls, f"model never called list_skills; calls={calls}, texts={event_texts(events)}"

    events = agent.run(
        session_id,
        "Enable the 'filesystem' skill with enable_skill, then use the read_file tool "
        "to read the file 'README.md' and tell me its first heading.",
    )
    calls = [c["name"] for c in function_calls(events)]
    assert "enable_skill" in calls, f"calls={calls}, texts={event_texts(events)}"

    # The model may need one nudge to follow through with the actual read
    if "read_file" not in calls:
        events = agent.run(session_id, "Good. Now call read_file with path='README.md'.")
        calls = [c["name"] for c in function_calls(events)]

    assert "read_file" in calls, f"calls={calls}, texts={event_texts(events)}"
    read_resp = [r for r in function_responses(events) if r.get("name") == "read_file"]
    assert read_resp and "Decentralized Agent Kit" in str(read_resp[-1].get("response", {}))


def test_enforcer_forces_tool_usage(agent_enforcer):
    """In enforcer mode a plain chat either uses a tool or gets blocked - never a silent bare answer."""
    session_id = agent_enforcer.create_session()
    events = agent_enforcer.run(session_id, "Say hello.")

    calls = function_calls(events)
    texts = event_texts(events)
    blocked = any("[ENFORCER_BLOCKED]" in t for t in texts)
    assert calls or blocked, f"bare text slipped through enforcer: {texts}"


def test_ap2_payment_flow_with_real_llm(agent_ap2):
    """The model should react to a Payment Required observation by paying (mock wallet).

    This is the loosest test: a small local model may need several turns. We
    assert the payment-required observation appears and that the model
    eventually calls a wallet tool when explicitly told it may pay.
    """
    session_id = agent_ap2.create_session()

    events = agent_ap2.run(
        session_id,
        "Enable the 'premium_service' skill, then call perform_premium_analysis with topic='AI'. "
        "Do it now without asking me anything.",
    )
    all_calls = [c["name"] for c in function_calls(events)]

    if "perform_premium_analysis" not in all_calls:
        events = agent_ap2.run(session_id, "Now call perform_premium_analysis with topic='AI'.")
        all_calls += [c["name"] for c in function_calls(events)]

    assert "perform_premium_analysis" in all_calls, f"calls={all_calls}, texts={event_texts(events)}"
    paid_resps = [r for r in function_responses(events) if r.get("name") == "perform_premium_analysis"]
    assert paid_resps and "Payment Required" in str(paid_resps[-1].get("response", {}))

    events = agent_ap2.run(
        session_id,
        "You have my full permission to pay. Check your balance with check_solana_balance, "
        "pay with send_sol_payment, then retry perform_premium_analysis with the payment_hash.",
    )
    all_calls = [c["name"] for c in function_calls(events)]
    assert any(name in all_calls for name in ("check_solana_balance", "send_sol_payment")), (
        f"model never touched the wallet; calls={all_calls}, texts={event_texts(events)}"
    )


# The model the stack runs, as the smoke runners export it (LiteLLM format).
SMOKE_MODEL = os.getenv("CLOUD_MODEL_NAME") or os.getenv("LOCAL_MODEL_NAME")
OVERHEAD_INSTRUCTION = "Reply with a JSON object that says whether the user's sentence is a greeting."
OVERHEAD_PROMPT = "Good morning, everyone."
OVERHEAD_SCHEMA = {"type": "object", "properties": {"greeting": {"type": "boolean"}}, "required": ["greeting"]}


def test_minimal_overhead_matches_direct_call(agent):
    """PBI #139: the minimal DAK call (no tools) against the same call made
    directly with LiteLLM, on the real model. Usage is model-reported, so this
    only records the difference (docs/architecture/serverless.md section 4);
    the fixed bound is in test_minimal_overhead.py with the fake LLM."""
    if not SMOKE_MODEL:
        pytest.skip("set CLOUD_MODEL_NAME or LOCAL_MODEL_NAME to the stack's model (the smoke runners export it)")
    litellm = pytest.importorskip("litellm", reason="install the smoke group: uv sync --group smoke")

    # What ADK sends for this output schema (google/adk/models/lite_llm.py, _to_litellm_response_format)
    strict_schema = {**OVERHEAD_SCHEMA, "additionalProperties": False}
    direct = litellm.completion(
        model=SMOKE_MODEL,
        messages=[{"role": "system", "content": OVERHEAD_INSTRUCTION}, {"role": "user", "content": OVERHEAD_PROMPT}],
        response_format={"type": "json_schema",
                         "json_schema": {"name": "response", "strict": True, "schema": strict_schema}},
    )

    resp = httpx.post(f"{AGENT_URL}/run", json={
        "app_name": APP_NAME,
        "user_id": agent.user_id,
        "session_id": agent.create_session(),
        "new_message": {"parts": [{"text": OVERHEAD_PROMPT}]},
        "state_delta": {"dak:instruction": OVERHEAD_INSTRUCTION, "dak:output_schema": OVERHEAD_SCHEMA,
                        "dak:tools": []},
    }, timeout=AGENT_RUN_TIMEOUT)
    resp.raise_for_status()
    usages = [e["usageMetadata"] for e in resp.json() if e.get("usageMetadata")]

    dak_in = sum(u.get("promptTokenCount", 0) for u in usages)
    dak_out = sum(u.get("candidatesTokenCount", 0) for u in usages)
    print(json.dumps({
        "model": SMOKE_MODEL,
        "direct": {"calls": 1, "prompt_tokens": direct.usage.prompt_tokens,
                   "completion_tokens": direct.usage.completion_tokens},
        "dak": {"calls": len(usages), "prompt_tokens": dak_in, "completion_tokens": dak_out},
        "diff": {"prompt_tokens": dak_in - direct.usage.prompt_tokens,
                 "completion_tokens": dak_out - direct.usage.completion_tokens},
        "dak_reply": event_texts(resp.json())[-1:],
    }))
    assert len(usages) == 1, usages
    assert dak_in > 0 and direct.usage.prompt_tokens > 0
