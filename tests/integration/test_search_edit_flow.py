"""Search then edit through the real stack: agent container → LiteLLM → fake
LLM, and the real mcp-server's grep / edit_file on a scratch file.

A session starts with built-in tools only; the model enables grep and
edit_file with enable_skill, as it does for any tool of the MCP server. The
mcp-server mounts the repo at /projects (its working directory), so the
scratch file is written and removed here. edit_file on the default MCP server
asks for approval (agent's permission rules); the test approves it through
/approvals the way a user would, and the turn resumes with the edit's result."""
import os
import uuid

import httpx
import pytest

from conftest import AGENT_RUN_TIMEOUT, AGENT_URL, FakeLlm, function_calls, function_responses

MODEL = "fake-default"
ENABLE = [FakeLlm.tool_call("enable_skill", skill_name=n) for n in ("grep", "edit_file")]
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


@pytest.fixture
def scratch():
    """A file in the repo root that the mcp-server sees under the same relative name."""
    name = f"tmp_search_edit_it_{uuid.uuid4().hex[:8]}.txt"
    path = os.path.join(REPO_ROOT, name)
    yield name, path
    if os.path.exists(path):
        os.remove(path)


def _responses(events: list, name: str) -> list:
    return [str(r.get("response", {})) for r in function_responses(events) if r.get("name") == name]


def _approve_edit(agent, session_id: str) -> list:
    """Approve the one pending edit_file call; return the resumed turn's events."""
    resp = httpx.get(f"{AGENT_URL}/approvals", params={"user_id": agent.user_id, "session_id": session_id},
                     timeout=30.0)
    resp.raise_for_status()
    [item] = resp.json()
    assert item["tool_name"] == "edit_file"
    reply = httpx.post(f"{AGENT_URL}/approvals/{item['id']}/reply",
                       json={"user_id": agent.user_id, "session_id": session_id, "mode": "once"},
                       timeout=AGENT_RUN_TIMEOUT)
    assert reply.status_code == 200, reply.text
    events = httpx.get(f"{AGENT_URL}/apps/dak_agent/users/{agent.user_id}/sessions/{session_id}", timeout=30.0)
    events.raise_for_status()
    return events.json()["events"]


def test_grep_then_edit_file_updates_content(agent, fake_llm, scratch):
    name, path = scratch
    with open(path, "w", encoding="utf-8") as f:
        f.write("alpha\nbeta target\ngamma\n")
    fake_llm.clear(MODEL)
    fake_llm.script(MODEL, [
        *ENABLE,
        fake_llm.tool_call("grep", pattern="target", path=name),
        fake_llm.tool_call("edit_file", path=name, old_string="target", new_string="TARGET"),
        fake_llm.text("Waiting for approval."),
        fake_llm.text("Found and edited."),
    ])

    session_id = agent.create_session()
    events = agent.run(session_id, f"grep for target in {name} and replace it with TARGET")

    [grep] = _responses(events, "grep")
    assert f"{name}:2:" in grep and "beta target" in grep
    assert "edit_file" in [c["name"] for c in function_calls(events)]
    with open(path, encoding="utf-8") as f:
        assert f.read() == "alpha\nbeta target\ngamma\n"  # not before the approval

    events = _approve_edit(agent, session_id)

    assert any("Replaced 1 occurrence" in r for r in _responses(events, "edit_file")), f"events: {events}"
    with open(path, encoding="utf-8") as f:
        assert f.read() == "alpha\nbeta TARGET\ngamma\n"


def test_grep_no_match_then_edit_file_refuses_ambiguous_replace(agent, fake_llm, scratch):
    name, path = scratch
    with open(path, "w", encoding="utf-8") as f:
        f.write("a b a\n")
    fake_llm.clear(MODEL)
    fake_llm.script(MODEL, [
        *ENABLE,
        fake_llm.tool_call("grep", pattern="zzz", path=name),
        fake_llm.tool_call("edit_file", path=name, old_string="a", new_string="X"),
        fake_llm.text("Waiting for approval."),
        fake_llm.text("Nothing unique to edit."),
    ])

    session_id = agent.create_session()
    events = agent.run(session_id, f"replace a with X in {name}")

    [grep] = _responses(events, "grep")
    assert "No matches" in grep

    events = _approve_edit(agent, session_id)

    refusals = [r for r in _responses(events, "edit_file") if "occurs 2 times" in r]
    assert refusals, f"events: {events}"
    with open(path, encoding="utf-8") as f:
        assert f.read() == "a b a\n"  # unchanged
