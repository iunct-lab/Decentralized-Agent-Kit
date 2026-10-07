"""dak-cli acp E2E: the official ACP client (acp.spawn_agent_process) starts
`dak-cli acp` the way an editor does and talks to the test agent through it
(PBI #314, docs/architecture/acp_adapter.md)."""
import asyncio
import json
import os
import uuid

import pytest
from acp import Client, spawn_agent_process, text_block
from acp.schema import AllowedOutcome, DeniedOutcome, RequestPermissionResponse

from conftest import AGENT_URL

MODEL = "fake-default"
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CLI_DIR = os.path.join(REPO_ROOT, "cli")

pytestmark = pytest.mark.skipif(not os.path.isdir(CLI_DIR), reason="cli directory not found")


class Editor(Client):
    """Stands in for the editor: records every session/update and answers
    permission requests with `answer` ("allow" / "reject"), or with "cancel"
    sends session/cancel and answers the pending request `cancelled`, as the
    spec asks of a client that cancels."""

    def __init__(self, answer="allow"):
        self.updates = []
        self.asked = []
        self.answer = answer
        self.conn = None

    async def session_update(self, session_id, update, **kwargs):
        self.updates.append(update)

    async def request_permission(self, session_id, tool_call, options, **kwargs):
        self.asked.append(tool_call)
        if self.answer == "cancel":
            await self.conn.cancel(session_id=session_id)
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        return RequestPermissionResponse(outcome=AllowedOutcome(outcome="selected", option_id=self.answer))

    def kinds(self):
        return [u.session_update for u in self.updates]


def _env(tmp_path):
    """A logged-in dak-cli with its own ~/.dak-cli (as in test_cli.py)."""
    config_dir = tmp_path / ".dak-cli"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(json.dumps({"username": "it_acp_user"}))
    return {**os.environ, "HOME": str(tmp_path), "DAK_AGENT_URL": AGENT_URL}


async def _turns(tmp_path, *texts, editor=None):
    """One dak-cli acp process, one session, one prompt per text."""
    editor = editor or Editor()
    async with spawn_agent_process(editor, "uv", "run", "dak-cli", "acp", cwd=CLI_DIR, env=_env(tmp_path)) as (conn, _):
        editor.conn = conn
        init = await asyncio.wait_for(conn.initialize(protocol_version=1), 120)
        session = await asyncio.wait_for(conn.new_session(cwd=str(tmp_path), mcp_servers=[]), 60)
        responses = [await asyncio.wait_for(conn.prompt(session_id=session.session_id, prompt=[text_block(t)]), 120)
                     for t in texts]
    return init, responses, editor


async def _one_turn(tmp_path, text):
    init, [response], editor = await _turns(tmp_path, text)
    return init, response, editor


def test_acp_round_trip(fake_llm, tmp_path):
    fake_llm.clear(MODEL)
    fake_llm.script(MODEL, [fake_llm.text("ACP round trip answer.")])

    init, response, editor = asyncio.run(_one_turn(tmp_path, "Hello via ACP"))

    assert init.protocol_version == 1
    assert response.stop_reason == "end_turn"
    assert editor.kinds() == ["agent_message_chunk"]
    assert editor.updates[0].content.text == "ACP round trip answer."


def test_acp_tool_call(fake_llm, tmp_path):
    """A tool of the default MCP server (list_files is allowed without confirmation)."""
    fake_llm.clear(MODEL)
    fake_llm.script(MODEL, [fake_llm.tool_call("enable_skill", skill_name="list_files"),
                            fake_llm.tool_call("list_files", path="cli"), fake_llm.text("Listed cli/.")])

    _, response, editor = asyncio.run(_one_turn(tmp_path, "What is in cli/?"))

    assert response.stop_reason == "end_turn"
    assert editor.kinds() == ["tool_call", "tool_call_update", "tool_call", "tool_call_update", "agent_message_chunk"]
    call, done = editor.updates[2], editor.updates[3]
    assert (call.title, call.status, call.raw_input) == ("list_files", "in_progress", {"path": "cli"})
    assert (done.tool_call_id, done.status) == (call.tool_call_id, "completed")
    assert "pyproject.toml" in done.content[0].content.text


def _write_file_script(fake_llm, name, *after):
    """The model enables write_file, calls it (the default MCP server asks for
    confirmation), then answers again in the same turn (acp_adapter.md §3.3)."""
    fake_llm.clear(MODEL)
    fake_llm.script(MODEL, [fake_llm.tool_call("enable_skill", skill_name="write_file"),
                            fake_llm.tool_call("write_file", path=name, content="written via ACP"),
                            fake_llm.text("Waiting for your approval."), *after])


@pytest.fixture
def written():
    """An empty file under acp-it/ that write_file overwrites (mcp-server writes into the repo, mounted
    at /projects). Made here, as test_search_edit_flow.py does: a file the container creates belongs to
    its user and the host may not read or remove it. Write into a directory: #543."""
    name = f"acp-it/{uuid.uuid4().hex[:8]}.txt"
    path = os.path.join(REPO_ROOT, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w").close()
    yield name
    os.remove(path)
    if not os.listdir(os.path.dirname(path)):
        os.rmdir(os.path.dirname(path))


def _content(name):
    with open(os.path.join(REPO_ROOT, name)) as f:
        return f.read()


def _write_call(editor):
    [call] = [u for u in editor.updates if u.session_update == "tool_call" and u.title == "write_file"]
    return call


def test_acp_permission_allow(fake_llm, tmp_path, written):
    _write_file_script(fake_llm, written, fake_llm.text("Wrote it."))

    _, [response], editor = asyncio.run(_turns(tmp_path, "Write a file", editor=Editor("allow")))

    assert response.stop_reason == "end_turn"
    call = _write_call(editor)
    [asked] = editor.asked
    assert (asked.tool_call_id, asked.title) == (call.tool_call_id, "write_file")
    done = [u for u in editor.updates if u.session_update == "tool_call_update" and u.tool_call_id == call.tool_call_id]
    assert [u.status for u in done] == ["pending", "completed"]
    assert _content(written) == "written via ACP"
    assert editor.updates[-1].content.text == "Wrote it."


def test_acp_permission_reject(fake_llm, tmp_path, written):
    _write_file_script(fake_llm, written, fake_llm.text("OK, I did not write it."))

    _, [response], editor = asyncio.run(_turns(tmp_path, "Write a file", editor=Editor("reject")))

    assert response.stop_reason == "end_turn"
    call = _write_call(editor)
    done = [u for u in editor.updates if u.session_update == "tool_call_update" and u.tool_call_id == call.tool_call_id]
    assert [u.status for u in done] == ["pending", "failed"]
    assert "denied_by_user" in json.dumps(done[-1].raw_output)
    assert _content(written) == ""
    assert editor.updates[-1].content.text == "OK, I did not write it."


def test_acp_cancel(fake_llm, tmp_path, written):
    """Cancelled while the permission request waits: the turn ends `cancelled`,
    nothing is written, and the next prompt goes through."""
    _write_file_script(fake_llm, written, fake_llm.text("Next prompt answered."))

    _, responses, editor = asyncio.run(_turns(tmp_path, "Write a file", "Never mind", editor=Editor("cancel")))

    assert [r.stop_reason for r in responses] == ["cancelled", "end_turn"]
    assert _content(written) == ""
    assert editor.updates[-1].content.text == "Next prompt answered."
