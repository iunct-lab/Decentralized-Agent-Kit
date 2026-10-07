"""dak-cli acp (docs/architecture/acp_adapter.md): ADK events -> ACP session/update.

The events are the ones recorded in §5 of that document, trimmed to the keys
the adapter reads."""
import asyncio
from unittest.mock import MagicMock, patch

import pytest
from acp import RequestError, resource_link_block, text_block

from acp.schema import AllowedOutcome, DeniedOutcome, RequestPermissionResponse

from src.acp_agent import DakAcpAgent, events_to_updates
from src.client import AgentClient, ApprovalError

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


def test_hook_rewrite_is_judged_by_the_result_it_wraps():
    rewritten = {"observation": "hook_rewrote_output",
                 "result": {"content": [{"type": "text", "text": "[redacted]"}], "isError": False}}
    [update] = events_to_updates(_result(rewritten))
    assert (update.status, update.content[0].content.text) == ("completed", "[redacted]")

    blocked = {"observation": "hook_rewrote_input", "original_args": {}, "updated_args": {},
               "result": {"error": "boom"}}
    [update] = events_to_updates(_result(blocked))
    assert update.status == "failed"


def test_confirmation_is_not_a_tool_call_and_the_waiting_call_stays_pending():
    assert events_to_updates(CONFIRMATION) == []
    [update] = events_to_updates(WAITING)
    assert (update.tool_call_id, update.status) == ("call_11accb99", "pending")


def test_model_error_becomes_a_message():
    event = {"author": "dak_agent", "errorCode": "SAFETY", "errorMessage": "blocked by the model"}
    [update] = events_to_updates(event)
    assert update.session_update == "agent_message_chunk"
    assert update.content.text == "[error SAFETY] blocked by the model"


def test_a_failed_turn_is_a_json_rpc_error():
    agent, conn, _ = _agent_with([TEXT, {"error": "RuntimeError: backend down", "error_details": {}}])
    with pytest.raises(RequestError) as e:
        asyncio.run(agent.prompt(prompt=[text_block("hi")], session_id="s1"))
    assert "backend down" in str(e.value.data)


def test_unknown_session_is_invalid_params():
    with pytest.raises(RequestError) as e:
        asyncio.run(DakAcpAgent().prompt(prompt=[text_block("hi")], session_id="nope"))
    assert e.value.code == RequestError.invalid_params().code


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
    def __init__(self, answer="allow"):
        self.updates = []
        self.asked = []
        self.answer = answer

    async def session_update(self, session_id, update, **kwargs):
        self.updates.append((session_id, update))

    async def request_permission(self, session_id, tool_call, options, **kwargs):
        self.asked.append((tool_call, options))
        outcome = DeniedOutcome(outcome="cancelled") if self.answer == "cancelled" \
            else AllowedOutcome(outcome="selected", option_id=self.answer)
        return RequestPermissionResponse(outcome=outcome)


def _agent_with(events, answer="allow"):
    agent = DakAcpAgent()
    conn = _Conn(answer)
    agent.on_connect(conn)
    client = MagicMock()
    client.stream_events.return_value = (e for e in events)  # a generator, as stream_events is
    agent._clients["s1"] = client
    return agent, conn, client


def test_prompt_streams_updates_and_ends_turn():
    agent, conn, client = _agent_with([STATE_ONLY, LIST_SKILLS_CALL, LIST_SKILLS_RESULT, TEXT])
    prompt = [text_block("what "), text_block("can you do"), resource_link_block("a.py", "file:///w/a.py")]

    response = asyncio.run(agent.prompt(prompt=prompt, session_id="s1"))

    assert response.stop_reason == "end_turn"
    client.stream_events.assert_called_once_with({"parts": [{"text": "what can you do\nfile:///w/a.py"}]})
    assert [u.session_update for _, u in conn.updates] == ["tool_call", "tool_call_update", "agent_message_chunk"]


PENDING = [{"id": "adk-a4f3", "kind": "approval", "tool_name": "write_file",
            "tool_args": {"path": "a.txt", "content": "x"}, "status": "pending"}]
DONE = _result({"content": [{"type": "text", "text": "Successfully wrote to a.txt"}], "isError": False})
DONE["content"]["parts"][0]["functionResponse"]["id"] = "call_11accb99"


def _approval_turn(answer, reply=None):
    agent, conn, client = _agent_with([CONFIRMATION, WAITING, TEXT], answer)
    client.list_approvals.side_effect = [PENDING, []]
    if isinstance(reply, Exception):
        client.reply_approval.side_effect = reply
    else:
        client.reply_approval.return_value = reply
    response = asyncio.run(agent.prompt(prompt=[text_block("write")], session_id="s1"))
    return response, conn, client


def test_allowed_permission_is_answered_once_and_the_turn_goes_on():
    response, conn, client = _approval_turn("allow", [DONE, TEXT])

    [(tool_call, options)] = conn.asked
    assert (tool_call.tool_call_id, tool_call.title, tool_call.status) == ("call_11accb99", "write_file", "pending")
    assert [(o.option_id, o.kind) for o in options] == [("allow", "allow_once"), ("reject", "reject_once")]
    client.reply_approval.assert_called_once_with("adk-a4f3", client.session_id, "once")
    assert response.stop_reason == "end_turn"
    done = [u for _, u in conn.updates if u.session_update == "tool_call_update"][-1]
    assert (done.tool_call_id, done.status) == ("call_11accb99", "completed")


def test_rejected_permission_is_answered_reject():
    response, _, client = _approval_turn("reject", [TEXT])

    client.reply_approval.assert_called_once_with("adk-a4f3", client.session_id, "reject")
    assert response.stop_reason == "end_turn"


def test_another_confirmation_in_the_reply_is_asked_too():
    agent, conn, client = _agent_with([CONFIRMATION, WAITING, TEXT])
    client.list_approvals.side_effect = [PENDING, [{**PENDING[0], "id": "adk-b"}]]
    client.reply_approval.side_effect = [
        {"status": "needs_approval", "response": [DONE, {**CONFIRMATION, "content": {"role": "model", "parts": [
            {"functionCall": {"id": "adk-b", "name": "adk_request_confirmation",
                              "args": {"originalFunctionCall": {"id": "call_2", "name": "write_file"}}}}]}}]},
        [TEXT]]

    assert asyncio.run(agent.prompt(prompt=[text_block("write")], session_id="s1")).stop_reason == "end_turn"
    assert [c.tool_call_id for c, _ in conn.asked] == ["call_11accb99", "call_2"]


def test_cancelled_permission_is_left_unanswered():
    response, _, client = _approval_turn("cancelled")

    client.reply_approval.assert_not_called()
    assert response.stop_reason == "cancelled"


def test_expired_approval_fails_the_call_and_ends_the_turn():
    response, conn, client = _approval_turn("allow", ApprovalError(409, '{"observation": "timed_out"}'))

    assert response.stop_reason == "end_turn"
    failed, said = conn.updates[-2][1], conn.updates[-1][1]
    assert (failed.tool_call_id, failed.status) == ("call_11accb99", "failed")
    assert "expired" in said.content.text
    assert client.list_approvals.call_count == 2  # read again: the agent went on and may have asked again


def test_a_confirmation_after_an_expired_one_is_asked():
    agent, conn, client = _agent_with([CONFIRMATION, WAITING, TEXT])
    client.list_approvals.side_effect = [PENDING, [{**PENDING[0], "id": "adk-later", "tool_name": "edit_file"}], []]
    client.reply_approval.side_effect = [ApprovalError(409, "timed_out"), [TEXT]]

    assert asyncio.run(agent.prompt(prompt=[text_block("write")], session_id="s1")).stop_reason == "end_turn"
    assert [c.title for c, _ in conn.asked] == ["write_file", "edit_file"]
    assert client.reply_approval.call_args.args[0] == "adk-later"


def test_cancel_stops_reading_the_stream():
    agent = DakAcpAgent()
    conn = _Conn()
    agent.on_connect(conn)
    read, running = [], {}

    def events(_message):
        for event in (LIST_SKILLS_CALL, LIST_SKILLS_RESULT, TEXT):
            read.append(event)
            if event is LIST_SKILLS_CALL:
                asyncio.run_coroutine_threadsafe(agent.cancel(session_id="s1"), running["loop"]).result()
            yield event

    client = MagicMock()
    client.stream_events.side_effect = events
    agent._clients["s1"] = client

    async def main():
        running["loop"] = asyncio.get_running_loop()
        return await agent.prompt(prompt=[text_block("go")], session_id="s1")

    assert asyncio.run(main()).stop_reason == "cancelled"
    assert conn.updates == []
    assert read == [LIST_SKILLS_CALL]  # the generator was left: the connection closes, the agent's turn stops


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
