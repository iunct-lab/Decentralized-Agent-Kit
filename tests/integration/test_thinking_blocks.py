"""A conversation with a model that returns signed thinking blocks keeps going
after the request's earlier parts change (PBI #765).

Claude 5.5 answers 400 when a thinking block is sent back after the system
instruction, the tools or an earlier turn changed. The fake LLM enforces that on
its Anthropic route (`anthropic/fake-claude`, POST /v1/messages). DAK changes
earlier parts on a mode switch, on compaction and when it elides old turns."""
import json
import uuid

import httpx

from conftest import APP_NAME, event_texts, function_calls

MODEL = "fake-claude"
META_MODEL = "fake-default"
STATE = {"dak:model": f"anthropic/{MODEL}"}
ORIGINAL_REQUEST = "Please tidy the notes folder, step by step."


def test_mode_switch_with_signed_thinking_continues(agent, fake_llm):
    fake_llm.clear(MODEL)
    fake_llm.clear(META_MODEL)
    fake_llm.script(MODEL, [
        fake_llm.thinking("a mode switch may help", fake_llm.tool_call("switch_mode", reason="focus", new_focus="read files")),
        fake_llm.thinking("now answer", fake_llm.text("switched and answered")),
    ])
    # mode_manager's meta call runs on the startup model, not the call's `dak:model`
    fake_llm.script(META_MODEL, [fake_llm.text(json.dumps({
        "instruction": "Focus on reading files.", "selected_tools": ["read_file", "switch_mode"], "selected_skills": []}))])

    events = agent.run(agent.create_session(STATE), "Look at the files.")

    assert "switch_mode" in [c["name"] for c in function_calls(events)], f"events: {events}"
    assert "switched and answered" in event_texts(events), f"events: {events}"


def test_compaction_with_signed_thinking_continues(agent_harness_eval, fake_llm):
    fake_llm.clear(MODEL)
    session_id = agent_harness_eval.create_session(STATE)
    fake_llm.script(MODEL, [fake_llm.thinking("first", fake_llm.tool_call("get_context_remaining")),
                            fake_llm.thinking("first answer", fake_llm.text("ack 0"))])
    agent_harness_eval.run(session_id, ORIGINAL_REQUEST)
    events = []
    for i in range(1, 5):
        # a tool call keeps the signed thinking in the history the next turns send back
        fake_llm.script(MODEL, [fake_llm.thinking(f"thinking {i}", fake_llm.tool_call("get_context_remaining")),
                                fake_llm.thinking(f"answer {i}", fake_llm.text(f"step {i} done"))])
        events = agent_harness_eval.run(session_id, f"continue step {i} ({uuid.uuid4().hex[:6]})")

    session = httpx.get(
        f"{agent_harness_eval.base_url}/apps/{APP_NAME}/users/{agent_harness_eval.user_id}/sessions/{session_id}",
        timeout=30.0,
    ).json()
    assert any((e.get("actions") or {}).get("compaction") for e in session["events"]), "no compaction happened"
    assert "step 4 done" in event_texts(events), f"events: {events}"
