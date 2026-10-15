"""PBI #135 acceptance criteria 1 and 7 (code side): the Lambda entry runs one
turn in a throwaway in-memory session, without a database, and keeps the
request text out of logs. Recording `BaseLlm` as in
`test_call_scoped_output_schema.py`."""
import logging
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from google.genai import types

from dak_agent import lambda_handler
from dak_agent.skill_registry import SkillRegistry


@pytest.fixture(autouse=True)
def no_remote_mcp_discovery():
    with patch("dak_agent.remote_tools.discover_remote_tools", AsyncMock(return_value={})):
        yield


def _llm(reply="hello back", error=None):
    from google.adk.models._capabilities import LlmCapabilities
    from google.adk.models.base_llm import BaseLlm
    from google.adk.models.llm_response import LlmResponse

    requests = []

    class RecordingLlm(BaseLlm):
        @property
        def capabilities(self) -> LlmCapabilities:
            return LlmCapabilities(output_schema_and_tools=True)

        async def generate_content_async(self, llm_request, stream=False):
            requests.append(llm_request)
            if error:
                raise error
            yield LlmResponse(content=types.Content(
                role="model", parts=[types.Part(text=reply)]))

    return RecordingLlm(model="recording"), requests


def _install(monkeypatch, llm, tools=()):
    from google.adk.apps import App

    from dak_agent.adaptive_agent import AdaptiveAgent

    agent = AdaptiveAgent(model=llm, name="dak_agent", instruction="Base.", tools=list(tools))
    agent.skill_registry = MagicMock(spec=SkillRegistry)
    agent.skill_registry.get_skill.return_value = None
    agent.skill_registry.list_skills.return_value = []
    monkeypatch.setattr(lambda_handler, "_app_cache", App(name="dak_agent", root_agent=agent))


def _text(request) -> str:
    return "\n".join(p.text for c in request.contents for p in c.parts or [] if p.text)


@pytest.mark.asyncio
async def test_reply_is_returned_as_output(monkeypatch):
    llm, _ = _llm("hello back")
    _install(monkeypatch, llm)
    assert await lambda_handler.run({"message": "hi"}) == {"status": "succeeded", "output": "hello back"}


@pytest.mark.asyncio
async def test_works_without_session_database(monkeypatch):
    monkeypatch.delenv("SESSION_SERVICE_URI", raising=False)
    llm, requests = _llm()
    _install(monkeypatch, llm)
    assert (await lambda_handler.run({"message": "hi"}))["status"] == "succeeded"
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_no_tools_declared_when_dak_tools_omitted(monkeypatch):
    llm, requests = _llm()
    _install(monkeypatch, llm)
    await lambda_handler.run({"message": "hi"})
    assert not (requests[0].config.tools or [])


@pytest.mark.asyncio
async def test_tools_are_not_offered_when_dak_tools_omitted_but_agent_has_one(monkeypatch):
    def lookup(q: str) -> str:
        """Look something up."""
        return q

    llm, requests = _llm()
    _install(monkeypatch, llm, tools=[lookup])
    await lambda_handler.run({"message": "hi"})
    await lambda_handler.run({"message": "hi", "state_delta": {"dak:tools": ["lookup"]}})
    offered = lambda r: [d.name for t in (r.config.tools or []) for d in (t.function_declarations or [])]
    assert "lookup" not in offered(requests[0])


@pytest.mark.asyncio
async def test_owned_state_keys_are_refused_like_run(monkeypatch):
    llm, requests = _llm()
    _install(monkeypatch, llm)
    event = {"message": "hi", "state_delta": {"dak_permission_always_rules": ["*"]}}
    assert await lambda_handler.run(event) == {"status": "failed", "error": "invalid_event"}
    assert requests == []


def test_handler_survives_repeated_calls_on_one_loop(monkeypatch):
    llm, _ = _llm("ok")
    _install(monkeypatch, llm)
    assert [lambda_handler.handler({"message": "hi"})["output"] for _ in range(3)] == ["ok"] * 3


@pytest.mark.asyncio
async def test_two_calls_do_not_share_conversation(monkeypatch):
    llm, requests = _llm()
    _install(monkeypatch, llm)
    await lambda_handler.run({"message": "first-secret"})
    await lambda_handler.run({"message": "second"})
    assert "first-secret" not in _text(requests[1])


@pytest.mark.asyncio
async def test_message_and_state_values_are_not_logged(monkeypatch, caplog):
    llm, _ = _llm(error=RuntimeError("boom"))
    _install(monkeypatch, llm)
    with caplog.at_level(logging.DEBUG):
        result = await lambda_handler.run(
            {"message": "my-message", "state_delta": {"dak:instruction": "my-instruction"}})
    assert result == {"status": "failed", "error": "RuntimeError"}
    assert "my-message" not in caplog.text
    assert "my-instruction" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("event", [
    {}, {"message": 1}, {"message": "hi", "state_delta": []}, None, "hi"])
async def test_invalid_event_does_not_call_the_llm(monkeypatch, event):
    llm, requests = _llm()
    _install(monkeypatch, llm)
    assert await lambda_handler.run(event) == {"status": "failed", "error": "invalid_event"}
    assert requests == []


def test_handler_is_a_sync_wrapper(monkeypatch):
    llm, _ = _llm("ok")
    _install(monkeypatch, llm)
    assert lambda_handler.handler({"message": "hi"}, None)["output"] == "ok"


def test_importing_the_module_does_not_load_the_agent():
    import subprocess
    import sys
    code = ("import sys, dak_agent.lambda_handler; "
            "sys.exit(1 if 'dak_agent.agent' in sys.modules else 0)")
    assert subprocess.run([sys.executable, "-c", code]).returncode == 0


class _FakeTable:
    """A dict-backed stand-in for the boto3 Table: the calls the job code makes."""

    def __init__(self):
        self.items = {}

    def get_item(self, Key, ConsistentRead=False):
        item = self.items.get((Key["pk"], Key["sk"]))
        return {"Item": dict(item)} if item else {}

    def put_item(self, Item, ConditionExpression=None):
        from botocore.exceptions import ClientError
        key = (Item["pk"], Item["sk"])
        if ConditionExpression and key in self.items:
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")
        self.items[key] = dict(Item)

    def update_item(self, Key, ExpressionAttributeNames, ExpressionAttributeValues, UpdateExpression=None):
        item = self.items[(Key["pk"], Key["sk"])]
        for placeholder, name in ExpressionAttributeNames.items():
            item[name] = ExpressionAttributeValues[":" + placeholder[1:]]


class _Context:
    def get_remaining_time_in_millis(self):
        return 60_000


@pytest.fixture
def table(monkeypatch):
    fake = _FakeTable()
    monkeypatch.setattr(lambda_handler, "_table", lambda: fake)
    return fake


@pytest.mark.asyncio
async def test_job_result_is_polled_with_get(monkeypatch, table):
    llm, _ = _llm("done")
    _install(monkeypatch, llm)
    event = {"message": "hi", "job_id": "j1"}
    assert await lambda_handler.run(event, _Context()) == {"status": "succeeded", "output": "done"}
    got = await lambda_handler.run({"op": "get", "job_id": "j1"})
    assert got == {"job_id": "j1", "status": "succeeded", "output": "done"}
    assert table.items[("J#j1", "JOB")]["expires_at"] > 0


@pytest.mark.asyncio
async def test_same_job_id_twice_does_not_call_the_llm_again(monkeypatch, table):
    llm, requests = _llm()
    _install(monkeypatch, llm)
    await lambda_handler.run({"message": "hi", "job_id": "j1"}, _Context())
    again = await lambda_handler.run({"message": "hi", "job_id": "j1"}, _Context())
    assert again == {"status": "failed", "error": "job_exists"}
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_running_job_past_its_deadline_is_timed_out(table):
    table.items[("J#j1", "JOB")] = {"pk": "J#j1", "sk": "JOB", "status": "running", "deadline": 1}
    assert (await lambda_handler.run({"op": "get", "job_id": "j1"}))["status"] == "timed_out"
    table.items[("J#j2", "JOB")] = {"pk": "J#j2", "sk": "JOB", "status": "running",
                                    "deadline": 9_999_999_999}
    assert (await lambda_handler.run({"op": "get", "job_id": "j2"}))["status"] == "running"


@pytest.mark.asyncio
async def test_unknown_job_is_not_found(table):
    assert await lambda_handler.run({"op": "get", "job_id": "nope"}) == {
        "job_id": "nope", "status": "not_found"}


@pytest.mark.asyncio
async def test_job_without_table_is_not_configured(monkeypatch):
    monkeypatch.delenv("DAK_TABLE", raising=False)
    llm, requests = _llm()
    _install(monkeypatch, llm)
    expected = {"status": "failed", "error": "table_not_configured"}
    assert await lambda_handler.run({"message": "hi", "job_id": "j1"}, _Context()) == expected
    assert await lambda_handler.run({"op": "get", "job_id": "j1"}) == expected
    assert requests == []


@pytest.mark.asyncio
async def test_failed_turn_and_oversized_output_are_recorded(monkeypatch, table):
    llm, _ = _llm(error=RuntimeError("boom"))
    _install(monkeypatch, llm)
    await lambda_handler.run({"message": "hi", "job_id": "bad"}, _Context())
    assert await lambda_handler.run({"op": "get", "job_id": "bad"}) == {
        "job_id": "bad", "status": "failed", "error": "RuntimeError"}

    llm, _ = _llm("x" * (lambda_handler.MAX_OUTPUT_BYTES + 1))
    _install(monkeypatch, llm)
    await lambda_handler.run({"message": "hi", "job_id": "big"}, _Context())
    got = await lambda_handler.run({"op": "get", "job_id": "big"})
    assert got["status"] == "failed" and got["error"] == "output_too_large" and "output" not in got


@pytest.mark.asyncio
async def test_expired_job_row_is_not_found(table):
    table.items[("J#old", "JOB")] = {"pk": "J#old", "sk": "JOB", "status": "succeeded",
                                     "output": "x", "expires_at": 1}
    assert (await lambda_handler.run({"op": "get", "job_id": "old"}))["status"] == "not_found"


@pytest.mark.asyncio
async def test_start_error_other_than_exists_is_a_failed_result(monkeypatch, table):
    from botocore.exceptions import ClientError

    def throttled(**kwargs):
        raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "PutItem")

    monkeypatch.setattr(table, "put_item", throttled)
    llm, requests = _llm()
    _install(monkeypatch, llm)
    result = await lambda_handler.run({"message": "hi", "job_id": "j1"}, _Context())
    assert result == {"status": "failed", "error": "ClientError"}
    assert requests == []


@pytest.mark.asyncio
async def test_finish_error_is_a_failed_result_and_the_row_stays_running(monkeypatch, table):
    def broken(**kwargs):
        raise RuntimeError("down")

    monkeypatch.setattr(table, "update_item", broken)
    llm, _ = _llm()
    _install(monkeypatch, llm)
    result = await lambda_handler.run({"message": "hi", "job_id": "j1"}, _Context())
    assert result == {"status": "failed", "error": "RuntimeError"}
    assert table.items[("J#j1", "JOB")]["status"] == "running"


class _FakeSsm:
    def __init__(self, value="the-key-value", error=None):
        self.calls, self.value, self.error = [], value, error

    def get_parameter(self, Name, WithDecryption):
        self.calls.append((Name, WithDecryption))
        if self.error:
            raise self.error
        return {"Parameter": {"Value": self.value}}


@pytest.fixture
def ssm(monkeypatch):
    fake = _FakeSsm()
    monkeypatch.setattr(lambda_handler, "_ssm", lambda: fake)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    yield fake
    os.environ.pop("ANTHROPIC_API_KEY", None)  # set by the code under test, not by monkeypatch


@pytest.mark.asyncio
async def test_api_key_is_read_from_the_parameter_once(monkeypatch, ssm):
    monkeypatch.setenv("DAK_API_KEY_PARAMETER", "/some/parameter")
    llm, _ = _llm()
    _install(monkeypatch, llm)
    await lambda_handler.run({"message": "hi"})
    await lambda_handler.run({"message": "hi"})
    assert os.environ["ANTHROPIC_API_KEY"] == "the-key-value"
    assert ssm.calls == [("/some/parameter", True)]


@pytest.mark.asyncio
async def test_no_parameter_name_reads_nothing(monkeypatch, ssm):
    monkeypatch.delenv("DAK_API_KEY_PARAMETER", raising=False)
    llm, _ = _llm()
    _install(monkeypatch, llm)
    assert (await lambda_handler.run({"message": "hi"}))["status"] == "succeeded"
    assert ssm.calls == []


@pytest.mark.asyncio
async def test_unreadable_parameter_fails_without_calling_the_llm(monkeypatch, caplog, ssm):
    monkeypatch.setenv("DAK_API_KEY_PARAMETER", "/some/parameter")
    ssm.error = RuntimeError("denied")
    llm, requests = _llm()
    _install(monkeypatch, llm)
    with caplog.at_level(logging.DEBUG):
        result = await lambda_handler.run({"message": "hi"})
    assert result == {"status": "failed", "error": "RuntimeError"}
    assert requests == []


@pytest.mark.asyncio
async def test_key_value_is_not_logged(monkeypatch, caplog, ssm):
    monkeypatch.setenv("DAK_API_KEY_PARAMETER", "/some/parameter")
    llm, _ = _llm()
    _install(monkeypatch, llm)
    with caplog.at_level(logging.DEBUG):
        await lambda_handler.run({"message": "hi"})
    assert "the-key-value" not in caplog.text
