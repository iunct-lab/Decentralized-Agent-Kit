"""PBI #137 acceptance criteria 2 and 3: a `dak:output_schema` passed with a
call puts a structured-output spec on that call's LLM request, and a reply
that does not match it comes back as a structured failure.
Same `Runner` + recording `BaseLlm` technique as
`test_call_scoped_instruction.py`."""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from google.genai import types

from dak_agent.skill_registry import SkillRegistry

SCHEMA = {"type": "object", "properties": {"date": {"type": "string"}}, "required": ["date"]}


@pytest.fixture(autouse=True)
def no_remote_mcp_discovery():
    with patch("dak_agent.remote_tools.discover_remote_tools", AsyncMock(return_value={})):
        yield


def _recording_llm(reply='{"date": "2026-09-22"}', replies=None):
    """Answers `replies` in turn (the last one repeats), or always `reply`;
    an exception in `replies` is raised instead."""
    from google.adk.models._capabilities import LlmCapabilities
    from google.adk.models.base_llm import BaseLlm
    from google.adk.models.llm_response import LlmResponse

    requests = []

    class RecordingLlm(BaseLlm):
        @property
        def capabilities(self) -> LlmCapabilities:
            # What DAK's LiteLlm reports: structured output alongside tools.
            return LlmCapabilities(output_schema_and_tools=True)

        async def generate_content_async(self, llm_request, stream=False):
            requests.append(llm_request)
            text = replies[min(len(requests), len(replies)) - 1] if replies else reply
            if isinstance(text, Exception):
                raise text
            yield LlmResponse(content=types.Content(
                role="model", parts=[types.Part(text=text)]))

    return RecordingLlm(model="recording"), requests


def _app(llm):
    from google.adk.apps import App

    from dak_agent.adaptive_agent import AdaptiveAgent

    agent = AdaptiveAgent(model=llm, name="dak_agent", instruction="Base instruction.", tools=[])
    agent.skill_registry = MagicMock(spec=SkillRegistry)
    agent.skill_registry.get_skill.return_value = None
    agent.skill_registry.list_skills.return_value = []
    return App(name="dak_agent", root_agent=agent)


async def _run(app, sessions, session_id, state_delta=None):
    """Returns the texts of the events the call produced."""
    from google.adk.artifacts import InMemoryArtifactService
    from google.adk.runners import Runner

    runner = Runner(app=app, session_service=sessions, artifact_service=InMemoryArtifactService())
    texts = []
    async for event in runner.run_async(
        user_id="u", session_id=session_id,
        new_message=types.Content(role="user", parts=[types.Part(text="hi")]),
        state_delta=state_delta,
    ):
        for part in (event.content.parts if event.content else []) or []:
            if part.text:
                texts.append(part.text)
    return texts


@pytest.mark.asyncio
async def test_call_output_schema_sets_structured_output_on_that_session_only():
    from google.adk.sessions import InMemorySessionService

    llm, requests = _recording_llm()
    app = _app(llm)
    sessions = InMemorySessionService()
    with_schema = await sessions.create_session(app_name="dak_agent", user_id="u")
    without_schema = await sessions.create_session(app_name="dak_agent", user_id="u")

    await _run(app, sessions, with_schema.id, state_delta={"dak:output_schema": SCHEMA})
    await _run(app, sessions, without_schema.id)

    config = requests[0].config
    assert config.response_mime_type == "application/json"
    schema = config.response_schema
    schema = schema if isinstance(schema, dict) else schema.model_dump(exclude_none=True)
    assert schema["required"] == ["date"]
    assert "date" in schema["properties"]

    assert requests[1].config.response_schema is None
    assert requests[1].config.response_mime_type is None


@pytest.mark.asyncio
async def test_reply_not_matching_output_schema_becomes_structured_failure():
    from google.adk.sessions import InMemorySessionService

    llm, requests = _recording_llm(reply='{"note": "missing date"}')
    app = _app(llm)
    sessions = InMemorySessionService()
    session = await sessions.create_session(app_name="dak_agent", user_id="u")

    texts = await _run(app, sessions, session.id, state_delta={"dak:output_schema": SCHEMA})

    # Regenerated up to the default limit, then the failure keeps #137's name.
    failure = json.loads(texts[-1])
    assert failure["error"] == "output_schema_validation_failed"
    assert [i["path"] for i in failure["issues"]] == ["date"]
    assert failure["attempts"] == len(requests) == 3
    assert failure["last_response"] == '{"note": "missing date"}'


@pytest.mark.asyncio
async def test_reply_matching_output_schema_is_returned_unchanged():
    from google.adk.sessions import InMemorySessionService

    llm, _ = _recording_llm(reply='{"date": "2026-09-22"}')
    app = _app(llm)
    sessions = InMemorySessionService()
    session = await sessions.create_session(app_name="dak_agent", user_id="u")

    texts = await _run(app, sessions, session.id, state_delta={"dak:output_schema": SCHEMA})

    assert texts[-1] == '{"date": "2026-09-22"}'


@pytest.mark.asyncio
async def test_reply_without_output_schema_is_not_validated():
    from google.adk.sessions import InMemorySessionService

    llm, _ = _recording_llm(reply="plain text, not JSON")
    app = _app(llm)
    sessions = InMemorySessionService()
    session = await sessions.create_session(app_name="dak_agent", user_id="u")

    assert (await _run(app, sessions, session.id))[-1] == "plain text, not JSON"


@pytest.mark.asyncio
async def test_empty_output_schema_still_requires_json():
    """`{}` accepts any JSON value, but a non-JSON reply must still fail."""
    from google.adk.sessions import InMemorySessionService

    llm, _ = _recording_llm(reply="plain text, not JSON")
    app = _app(llm)
    sessions = InMemorySessionService()
    session = await sessions.create_session(app_name="dak_agent", user_id="u")

    texts = await _run(app, sessions, session.id, state_delta={"dak:output_schema": {}})

    assert json.loads(texts[-1])["error"] == "output_schema_validation_failed"


INSPECTION = {"dak:inspection": {"json_schema": SCHEMA}}


async def _inspected_run(replies, state_delta=INSPECTION):
    from google.adk.sessions import InMemorySessionService

    llm, requests = _recording_llm(replies=replies)
    app = _app(llm)
    sessions = InMemorySessionService()
    session = await sessions.create_session(app_name="dak_agent", user_id="u")
    return await _run(app, sessions, session.id, state_delta=state_delta), requests


def _request_text(request) -> str:
    return "\n".join(p.text for c in request.contents for p in c.parts or [] if p.text)


@pytest.mark.asyncio
async def test_inspection_passing_on_first_try_calls_the_llm_once():
    texts, requests = await _inspected_run(['{"date": "2026-09-22"}'])

    assert texts[-1] == '{"date": "2026-09-22"}'
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_inspection_retry_succeeds_on_second_attempt():
    texts, requests = await _inspected_run(['{"note": "missing date"}', '{"date": "2026-09-22"}'])

    assert texts[-1] == '{"date": "2026-09-22"}'
    assert len(requests) == 2
    retry = _request_text(requests[1])
    # The user's question, the failed reply and the inspection's error.
    assert "hi" in retry and '{"note": "missing date"}' in retry and "'date' is a required property" in retry
    assert "Base instruction." in requests[1].config.system_instruction


@pytest.mark.asyncio
async def test_inspection_retry_gives_up_after_limit(monkeypatch):
    monkeypatch.delenv("DAK_MAX_LLM_CALLS", raising=False)
    texts, requests = await _inspected_run(['{"note": "missing date"}'], {**INSPECTION, "dak:max_llm_calls": 2})

    failure = json.loads(texts[-1])
    assert failure["error"] == "inspection_failed"
    assert failure["attempts"] == len(requests) == 2
    assert failure["last_response"] == '{"note": "missing date"}'
    assert [i["path"] for i in failure["issues"]] == ["date"]


@pytest.mark.asyncio
async def test_inspection_retry_fails_closed_when_the_model_call_raises():
    texts, requests = await _inspected_run(['{"note": "missing date"}', RuntimeError("model down")])

    failure = json.loads(texts[-1])
    assert failure["error"] == "inspection_failed"
    assert "model down" in failure["issues"][-1]["message"]


@pytest.mark.asyncio
async def test_inspection_endpoint_not_allowed_is_refused_before_any_llm_call(monkeypatch):
    monkeypatch.delenv("DAK_ALLOWED_INSPECTION_URLS", raising=False)
    texts, requests = await _inspected_run(['{"date": "x"}'], {"dak:inspection": {"http": {"url": "http://caller/v"}}})

    assert json.loads(texts[-1])["error"] == "inspection_url_not_allowed"
    assert requests == []


class TestReplyEligibility:
    """Only a complete, final text reply is validated (`_retry_on_inspection_failure`)."""

    def _check(self, response, schema=SCHEMA):
        import asyncio

        from dak_agent.adaptive_agent import AdaptiveAgent

        agent = AdaptiveAgent(model="test-model", name="dak_agent", instruction="x", tools=[])
        return asyncio.run(agent._retry_on_inspection_failure(response, MagicMock(), {"dak:output_schema": schema}))

    @staticmethod
    def _response(*parts, partial=None):
        from google.adk.models.llm_response import LlmResponse

        return LlmResponse(content=types.Content(role="model", parts=list(parts)), partial=partial)

    def test_tool_call_turn_is_not_validated(self):
        call = types.Part(function_call=types.FunctionCall(name="t", args={}))
        assert self._check(self._response(types.Part(text="let me look"), call)) is None

    def test_partial_stream_chunk_is_not_validated(self):
        assert self._check(self._response(types.Part(text='{"da'), partial=True)) is None

    def test_thought_text_is_left_out(self):
        reply = self._response(types.Part(text="thinking...", thought=True),
                               types.Part(text='{"date": "2026-09-22"}'))
        assert self._check(reply) is None

    def test_validation_error_fails_closed(self):
        """An unexpected error while validating must not let the reply through."""
        with patch("dak_agent.call_config.validate_call_output", side_effect=RuntimeError("boom")):
            failure = self._check(self._response(types.Part(text='{"date": "x"}')))
        assert json.loads(failure.content.parts[0].text)["error"] == "output_schema_validation_failed"
