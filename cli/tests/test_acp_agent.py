"""dak-cli acp (docs/architecture/acp_adapter.md): ADK events -> ACP session/update.

The events are the ones recorded in §5 of that document, trimmed to the keys
the adapter reads."""
import asyncio
from unittest.mock import MagicMock, patch

import pytest
from acp import RequestError, resource_link_block, text_block

from src.acp_agent import DakAcpAgent, events_to_updates
from src.client import AgentClient

STATE_ONLY = {"author": "dak_agent", "actions": {"stateDelta": {"dak_original_request": "hi"}}}
TEXT = {"author": "dak_agent", "partial": False,
        "content": {"role": "model", "parts": [{"text": "Hello from DAK."}]}}
LIST_SKILLS_CALL = {"author": "dak_agent", "content": {"role": "model", "parts": [
    {"functionCall": {"id": "call_eebd365b", "name": "list_skills", "args": {}}}]}}
LIST_SKILLS_RESULT = {"author": "dak_agent", "content": {"role": "user", "parts": [
    {"functionResponse": {"id": "call_eebd365b", "name": "list_skills",
                          "response": {"result": "## Curated Skills (Recommended)\n- filesystem"}}}]}}
CONFIRMATION = {"author": "dak_agent", "longRunningToolIds": ["adk-a4f3"], "content": {"role": "model", "parts": [
    {"functionCall": {"id": "adk-a4f3", "name": "adk_request_confirmation", "args": {
        "originalFunctionCall": {"id": "call_11accb99", "name": "write_file", "args": {"path": "a.txt", "content": "x"}},
        "toolConfirmation": {"hint": "Please approve or reject", "confirmed": False}}}}]}}
WAITING = {"author": "dak_agent",
           "content": {"role": "user", "parts": [{"functionResponse": {
               "id": "call_11accb99", "name": "write_file",
               "response": {"error": "This tool call requires confirmation, please approve or reject."}}}]},
           "actions": {"requestedToolConfirmations": {"call_11accb99": {"hint": "…", "confirmed": False}}}}


def _result(response):
    return {"author": "dak_agent", "content": {"role": "user", "parts": [
        {"functionResponse": {"id": "call_1", "name": "write_file", "response": response}}]}}


def test_text_becomes_agent_message_chunk():
    assert events_to_updates(STATE_ONLY) == []
    [update] = events_to_updates(TEXT)
    assert update.session_update == "agent_message_chunk"
    assert update.content.text == "Hello from DAK."


def test_thought_becomes_agent_thought_chunk():
    event = {"author": "dak_agent", "content": {"role": "model", "parts": [{"text": "hmm", "thought": True}]}}
    [update] = events_to_updates(event)
    assert (update.session_update, update.content.text) == ("agent_thought_chunk", "hmm")


def test_function_call_and_response_become_tool_call_and_update():
    [start] = events_to_updates(LIST_SKILLS_CALL)
    assert (start.session_update, start.tool_call_id, start.title, start.status, start.raw_input) == \
        ("tool_call", "call_eebd365b", "list_skills", "in_progress", {})

    [done] = events_to_updates(LIST_SKILLS_RESULT)
    assert (done.session_update, done.tool_call_id, done.status) == ("tool_call_update", "call_eebd365b", "completed")
    assert done.content[0].content.text.startswith("## Curated Skills")
    assert done.raw_output == {"result": "## Curated Skills (Recommended)\n- filesystem"}


def test_mcp_result_text_and_failures():
    ok = _result({"content": [{"type": "text", "text": "Successfully wrote to a/b.txt"}], "isError": False})
    [update] = events_to_updates(ok)
    assert (update.status, update.content[0].content.text) == ("completed", "Successfully wrote to a/b.txt")

    for response in ({"content": [{"type": "text", "text": "boom"}], "isError": True},
                     {"error": "something broke"},
                     {"observation": "denied_by_user", "reason": ""}):
        [update] = events_to_updates(_result(response))
        assert update.status == "failed", response


def test_confirmation_is_not_a_tool_call_and_the_waiting_call_stays_pending():
    assert events_to_updates(CONFIRMATION) == []
    [update] = events_to_updates(WAITING)
    assert (update.tool_call_id, update.status) == ("call_11accb99", "pending")


@patch("src.client.requests.post")
@patch("src.client.ConfigManager")
def test_stream_events_reads_sse_data_lines(mock_config_class, mock_post):
    mock_config_class.return_value.get_agent_url.return_value = "http://agent:8000"
    mock_config_class.return_value.get_user.return_value = "u1"
    response = mock_post.return_value.__enter__.return_value
    response.iter_lines.return_value = ['data: {"author": "dak_agent"}', "", 'data: {"author": "x"}']
    client = AgentClient(session_id="s1")

    events = list(client.stream_events({"parts": [{"text": "hi"}]}))

    assert events == [{"author": "dak_agent"}, {"author": "x"}]
    url, kwargs = mock_post.call_args.args[0], mock_post.call_args.kwargs
    assert url == "http://agent:8000/run_sse"
    assert kwargs["stream"] is True
    assert kwargs["json"] == {"app_name": "dak_agent", "user_id": "u1", "session_id": "s1",
                              "new_message": {"parts": [{"text": "hi"}]}, "streaming": False}


class _Conn:
    def __init__(self):
        self.updates = []

    async def session_update(self, session_id, update, **kwargs):
        self.updates.append((session_id, update))


def _agent_with(events):
    agent = DakAcpAgent()
    conn = _Conn()
    agent.on_connect(conn)
    client = MagicMock()
    client.stream_events.return_value = iter(events)
    agent._clients["s1"] = client
    return agent, conn, client


def test_prompt_streams_updates_and_ends_turn():
    agent, conn, client = _agent_with([STATE_ONLY, LIST_SKILLS_CALL, LIST_SKILLS_RESULT, TEXT])
    prompt = [text_block("what "), text_block("can you do"), resource_link_block("a.py", "file:///w/a.py")]

    response = asyncio.run(agent.prompt(prompt=prompt, session_id="s1"))

    assert response.stop_reason == "end_turn"
    client.stream_events.assert_called_once_with({"parts": [{"text": "what can you do\nfile:///w/a.py"}]})
    assert [u.session_update for _, u in conn.updates] == ["tool_call", "tool_call_update", "agent_message_chunk"]


def test_prompt_says_approval_is_not_available_yet():
    agent, conn, _ = _agent_with([CONFIRMATION, WAITING])

    assert asyncio.run(agent.prompt(prompt=[text_block("write")], session_id="s1")).stop_reason == "end_turn"
    last = conn.updates[-1][1]
    assert last.session_update == "agent_message_chunk" and "write_file" in last.content.text


@patch("src.acp_agent.AgentClient")
def test_new_session_needs_login(mock_client_class):
    mock_client_class.return_value.username = None
    with pytest.raises(RequestError):
        asyncio.run(DakAcpAgent().new_session(cwd="/w", mcp_servers=[]))


@patch("src.acp_agent.AgentClient")
def test_new_session_uses_the_dak_session_id(mock_client_class):
    client = mock_client_class.return_value
    client.username, client.session_id = "u1", "dak-session-1"
    agent = DakAcpAgent()

    response = asyncio.run(agent.new_session(cwd="/w", mcp_servers=[]))

    client._ensure_session.assert_called_once_with()
    assert response.session_id == "dak-session-1"
    assert agent._clients["dak-session-1"] is client
