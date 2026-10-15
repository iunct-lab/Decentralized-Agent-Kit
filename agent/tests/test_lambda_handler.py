"""PBI #135 acceptance criteria 1 and 7 (code side): the Lambda entry runs one
turn in a throwaway in-memory session, without a database, and keeps the
request text out of logs. Recording `BaseLlm` as in
`test_call_scoped_output_schema.py`."""
import logging
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
