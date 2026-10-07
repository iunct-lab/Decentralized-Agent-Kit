"""What the agent sends the model after its history was compacted.

`agent-harness-eval` compacts every 3 invocations (DAK_COMPACTION_INTERVAL=3).
After 5 turns the first one has been summarised away, yet the request must
still carry the session's original request, and the tool definitions must
reach the model in the order recorded in golden/snapshots/tool_order.json.

The snapshot changes only on purpose:
    DAK_UPDATE_SNAPSHOTS=1 uv run pytest test_request_snapshot.py -q
rewrites it (and skips); review the diff and commit it. Otherwise any
difference fails the test.
"""
import json
import os
import uuid

import httpx
import pytest

from conftest import APP_NAME, system_instruction, tool_names

MODEL = "fake-harness"
ORIGINAL_REQUEST = "Investigate the repository structure and report back."
SNAPSHOTS = os.path.join(os.path.dirname(__file__), "golden", "snapshots")


def _compare_or_update_snapshot(path: str, value) -> None:
    if os.getenv("DAK_UPDATE_SNAPSHOTS") == "1":
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(value, f, indent=2)
            f.write("\n")
        pytest.skip("snapshot updated")
    with open(path) as f:
        expected = json.load(f)
    assert value == expected, f"{path} differs; if intended, rerun with DAK_UPDATE_SNAPSHOTS=1 and commit it"


def _user_texts(entry: dict) -> list:
    texts = []
    for message in entry["messages"]:
        if message.get("role") != "user":
            continue
        content = message.get("content")
        texts += [content] if isinstance(content, str) else [p.get("text", "") for p in content or []]
    return texts


def test_compaction_preserves_original_request_and_tool_order(agent_harness_eval, fake_llm):
    fake_llm.clear(MODEL)
    session_id = agent_harness_eval.create_session()
    fake_llm.script(MODEL, [{"text": "ack 0"}])
    agent_harness_eval.run(session_id, ORIGINAL_REQUEST)
    for i in range(1, 5):
        fake_llm.script(MODEL, [{"text": f"step {i} done"}])
        agent_harness_eval.run(session_id, f"continue step {i} ({uuid.uuid4().hex[:6]})")

    session = httpx.get(
        f"{agent_harness_eval.base_url}/apps/{APP_NAME}/users/{agent_harness_eval.user_id}/sessions/{session_id}",
        timeout=30.0,
    ).json()
    assert any((e.get("actions") or {}).get("compaction") for e in session["events"]), "no compaction happened"

    # The summarizer calls the same model without tools; the agent's own call carries them.
    last = [entry for entry in fake_llm.requests(MODEL) if entry["tools"]][-1]
    assert not any(ORIGINAL_REQUEST in text for text in _user_texts(last)), "the first turn was not compacted away"
    assert ORIGINAL_REQUEST in system_instruction(last)

    _compare_or_update_snapshot(os.path.join(SNAPSHOTS, "tool_order.json"), tool_names(last))
