"""dak-cli acp E2E: the official ACP client (acp.spawn_agent_process) starts
`dak-cli acp` the way an editor does and talks to the test agent through it
(PBI #314, docs/architecture/acp_adapter.md)."""
import asyncio
import json
import os

import pytest
from acp import Client, spawn_agent_process, text_block

from conftest import AGENT_URL

MODEL = "fake-default"
CLI_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "cli"))

pytestmark = pytest.mark.skipif(not os.path.isdir(CLI_DIR), reason="cli directory not found")


class Editor(Client):
    """Stands in for the editor: records every session/update."""

    def __init__(self):
        self.updates = []

    async def session_update(self, session_id, update, **kwargs):
        self.updates.append(update)

    def kinds(self):
        return [u.session_update for u in self.updates]


def _env(tmp_path):
    """A logged-in dak-cli with its own ~/.dak-cli (as in test_cli.py)."""
    config_dir = tmp_path / ".dak-cli"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(json.dumps({"username": "it_acp_user"}))
    return {**os.environ, "HOME": str(tmp_path), "DAK_AGENT_URL": AGENT_URL}


async def _one_turn(tmp_path, text):
    editor = Editor()
    async with spawn_agent_process(editor, "uv", "run", "dak-cli", "acp", cwd=CLI_DIR, env=_env(tmp_path)) as (conn, _):
        init = await asyncio.wait_for(conn.initialize(protocol_version=1), 120)
        session = await asyncio.wait_for(conn.new_session(cwd=str(tmp_path), mcp_servers=[]), 60)
        response = await asyncio.wait_for(conn.prompt(session_id=session.session_id, prompt=[text_block(text)]), 120)
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
    fake_llm.clear(MODEL)
    fake_llm.script(MODEL, [fake_llm.tool_call("list_skills"), fake_llm.text("Here is what I can do.")])

    _, response, editor = asyncio.run(_one_turn(tmp_path, "What can you do?"))

    assert response.stop_reason == "end_turn"
    assert editor.kinds() == ["tool_call", "tool_call_update", "agent_message_chunk"]
    call, done, _ = editor.updates
    assert (call.title, call.status) == ("list_skills", "in_progress")
    assert (done.tool_call_id, done.status) == (call.tool_call_id, "completed")
    assert "Curated Skills" in done.content[0].content.text
