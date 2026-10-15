"""PBI #721 acceptance criterion 1 (storage side), Task #720: a session kept in
DynamoDB continues across turns. A dict-backed fake table stands in for boto3's
`Table`; the 2-turn test drives a real ADK `Runner` with a recording `BaseLlm`
(same technique as `test_call_scoped_output_schema.py`)."""
import time

import pytest
from botocore.exceptions import ClientError
from google.adk.events.event import Event
from google.adk.events.event_actions import EventActions
from google.adk.sessions.base_session_service import GetSessionConfig
from google.genai import types

from dak_agent.dynamodb_sessions import DynamoDbSessionService

APP = "dak_agent"


class FakeTable:
    """Implements the boto3 Table calls the service uses, for the two
    KeyConditionExpression shapes it sends."""

    def __init__(self):
        self.items: dict[tuple[str, str], dict] = {}
        self.writes = 0

    def put_item(self, Item, ConditionExpression=None, ExpressionAttributeValues=None):
        if ConditionExpression:
            assert ConditionExpression == "attribute_not_exists(pk) OR ver = :ver"
            cur = self.items.get((Item["pk"], Item["sk"]))
            if cur is not None and cur["ver"] != ExpressionAttributeValues[":ver"]:
                raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")
        self.writes += 1
        self.items[(Item["pk"], Item["sk"])] = dict(Item)

    def get_item(self, Key, ConsistentRead=False):
        assert ConsistentRead is True
        item = self.items.get((Key["pk"], Key["sk"]))
        return {"Item": dict(item)} if item else {}

    def delete_item(self, Key):
        self.items.pop((Key["pk"], Key["sk"]), None)

    def query(self, KeyConditionExpression, ExpressionAttributeValues, ScanIndexForward=True,
              Limit=None, ExclusiveStartKey=None, ConsistentRead=False):
        assert ConsistentRead is True
        assert KeyConditionExpression == "pk = :pk AND sk BETWEEN :lo AND :hi"
        v = ExpressionAttributeValues
        rows = sorted(
            (i for (pk, sk), i in self.items.items() if pk == v[":pk"] and v[":lo"] <= sk <= v[":hi"]),
            key=lambda i: i["sk"],
            reverse=not ScanIndexForward,
        )
        return {"Items": rows[:Limit] if Limit else rows}


def _event(text, ts, author="user", delta=None):
    return Event(
        author=author,
        timestamp=ts,
        content=types.Content(role="user" if author == "user" else "model",
                              parts=[types.Part(text=text)]),
        actions=EventActions(state_delta=delta or {}),
    )


@pytest.fixture
def svc():
    return DynamoDbSessionService(FakeTable())


@pytest.mark.asyncio
async def test_create_then_read_returns_state_and_events(svc):
    s = await svc.create_session(app_name=APP, user_id="u", session_id="s1", state={"k": "v"})
    await svc.append_event(s, _event("hello", 100.0, delta={"n": 1}))
    await svc.append_event(s, _event("hi", 101.0, author="agent"))

    got = await svc.get_session(app_name=APP, user_id="u", session_id="s1")

    assert got.state == {"k": "v", "n": 1}
    assert [e.content.parts[0].text for e in got.events] == ["hello", "hi"]
    assert got.last_update_time == 101.0
    assert await svc.get_session(app_name=APP, user_id="u", session_id="nope") is None


@pytest.mark.asyncio
async def test_user_state_is_shared_across_sessions_and_app_state_across_users(svc):
    a = await svc.create_session(app_name=APP, user_id="u", session_id="a")
    await svc.append_event(a, _event("x", 1.0, delta={"user:name": "kai", "app:flag": True}))

    b = await svc.create_session(app_name=APP, user_id="u", session_id="b")
    other = await svc.create_session(app_name=APP, user_id="w", session_id="c")

    assert b.state["user:name"] == "kai" and b.state["app:flag"] is True
    assert "user:name" not in other.state and other.state["app:flag"] is True
    assert await svc.get_user_state(app_name=APP, user_id="u") == {"name": "kai"}
    assert [s.id for s in (await svc.list_sessions(app_name=APP, user_id="u")).sessions] == ["a", "b"]


@pytest.mark.asyncio
async def test_temp_state_is_not_persisted(svc):
    s = await svc.create_session(app_name=APP, user_id="u", session_id="s")
    await svc.append_event(s, _event("x", 1.0, delta={"temp:scratch": 1, "kept": 2}))

    got = await svc.get_session(app_name=APP, user_id="u", session_id="s")

    assert got.state == {"kept": 2}
    assert got.events[0].actions.state_delta == {"kept": 2}
    assert not any("scratch" in str(i) for i in svc._table.items.values())


@pytest.mark.asyncio
async def test_get_session_config_limits_events(svc):
    s = await svc.create_session(app_name=APP, user_id="u", session_id="s")
    for i in range(5):
        await svc.append_event(s, _event(f"m{i}", 10.0 + i))
    texts = lambda sess: [e.content.parts[0].text for e in sess.events]  # noqa: E731

    recent = await svc.get_session(app_name=APP, user_id="u", session_id="s",
                                   config=GetSessionConfig(num_recent_events=2))
    after = await svc.get_session(app_name=APP, user_id="u", session_id="s",
                                  config=GetSessionConfig(after_timestamp=12.0))
    none = await svc.get_session(app_name=APP, user_id="u", session_id="s",
                                 config=GetSessionConfig(num_recent_events=0))

    assert texts(recent) == ["m3", "m4"]
    assert texts(after) == ["m2", "m3", "m4"]
    assert none.events == []


@pytest.mark.asyncio
async def test_event_too_large_is_rejected_and_not_written(svc):
    s = await svc.create_session(app_name=APP, user_id="u", session_id="s")
    before = dict(svc._table.items)

    with pytest.raises(ValueError, match="event_too_large"):
        await svc.append_event(s, _event("x" * (400 * 1024), 1.0))

    assert svc._table.items == before


@pytest.mark.asyncio
async def test_list_requires_user_and_delete_removes_everything(svc):
    with pytest.raises(ValueError):
        await svc.list_sessions(app_name=APP)
    s = await svc.create_session(app_name=APP, user_id="u", session_id="s")
    await svc.append_event(s, _event("x", 1.0))

    await svc.delete_session(app_name=APP, user_id="u", session_id="s")

    assert await svc.get_session(app_name=APP, user_id="u", session_id="s") is None
    assert (await svc.list_sessions(app_name=APP, user_id="u")).sessions == []
    assert svc._table.items == {}


@pytest.mark.asyncio
async def test_expired_session_reads_as_missing():
    table = FakeTable()
    svc = DynamoDbSessionService(table, ttl_seconds=-1)
    await svc.create_session(app_name=APP, user_id="u", session_id="s")

    assert await svc.get_session(app_name=APP, user_id="u", session_id="s") is None


@pytest.mark.asyncio
async def test_second_turn_request_carries_first_turn_history():
    """Two `Runner` turns against the table: the 2nd LLM request contains the
    1st turn's input and reply."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from google.adk.apps import App
    from google.adk.artifacts import InMemoryArtifactService
    from google.adk.models._capabilities import LlmCapabilities
    from google.adk.models.base_llm import BaseLlm
    from google.adk.models.llm_response import LlmResponse
    from google.adk.runners import Runner

    from dak_agent.adaptive_agent import AdaptiveAgent
    from dak_agent.skill_registry import SkillRegistry

    requests = []

    class RecordingLlm(BaseLlm):
        @property
        def capabilities(self) -> LlmCapabilities:
            return LlmCapabilities(output_schema_and_tools=True)

        async def generate_content_async(self, llm_request, stream=False):
            requests.append(llm_request)
            yield LlmResponse(content=types.Content(
                role="model", parts=[types.Part(text=f"reply{len(requests)}")]))

    with patch("dak_agent.remote_tools.discover_remote_tools", AsyncMock(return_value={})):
        agent = AdaptiveAgent(model=RecordingLlm(model="recording"), name="dak_agent",
                              instruction="Base.", tools=[])
        agent.skill_registry = MagicMock(spec=SkillRegistry)
        agent.skill_registry.get_skill.return_value = None
        agent.skill_registry.list_skills.return_value = []
        app = App(name="dak_agent", root_agent=agent)
        sessions = DynamoDbSessionService(FakeTable())
        await sessions.create_session(app_name="dak_agent", user_id="u", session_id="s")

        for text in ("first question", "second question"):
            # A new Runner per turn, like a new Lambda invocation.
            runner = Runner(app=app, session_service=sessions,
                            artifact_service=InMemoryArtifactService())
            async for _ in runner.run_async(
                user_id="u", session_id="s",
                new_message=types.Content(role="user", parts=[types.Part(text=text)]),
            ):
                pass

    sent = [p.text for c in requests[1].contents for p in (c.parts or []) if p.text]
    assert "first question" in sent and "reply1" in sent and "second question" in sent


@pytest.mark.asyncio
async def test_keys_with_separators_do_not_collide_across_users(svc):
    await svc.create_session(app_name=APP, user_id="alice", session_id="x#s")
    await svc.create_session(app_name=APP, user_id="alice#x", session_id="s")

    assert (await svc.get_session(app_name=APP, user_id="alice", session_id="s")) is None
    await svc.delete_session(app_name=APP, user_id="alice#x", session_id="s")
    assert await svc.get_session(app_name=APP, user_id="alice", session_id="x#s") is not None


@pytest.mark.asyncio
async def test_rejected_event_leaves_session_untouched(svc):
    s = await svc.create_session(app_name=APP, user_id="u", session_id="s")
    big = _event("x" * (400 * 1024), 1.0, delta={"leak": 1})

    with pytest.raises(ValueError, match="event_too_large"):
        await svc.append_event(s, big)

    assert s.events == [] and "leak" not in s.state


@pytest.mark.asyncio
async def test_concurrent_user_state_updates_are_not_lost(svc):
    """A second writer that read the same STATE version retries instead of
    overwriting the first writer's key."""
    a = await svc.create_session(app_name=APP, user_id="u", session_id="a")
    b = await svc.create_session(app_name=APP, user_id="u", session_id="b")
    real_get = svc._get_raw
    raced = []

    def racing_get(pk, sk):
        item = real_get(pk, sk)
        if sk == "STATE" and not raced:  # another session writes between read and put
            raced.append(1)
            other = DynamoDbSessionService(svc._table)
            other._put_scoped_state(pk, {"from_a": 1}, time.time())
        return item

    svc._get_raw = racing_get
    await svc.append_event(b, _event("x", 2.0, delta={"user:from_b": 2}))
    svc._get_raw = real_get

    assert await svc.get_user_state(app_name=APP, user_id="u") == {"from_a": 1, "from_b": 2}


@pytest.mark.asyncio
async def test_expired_user_state_not_yet_deleted_can_be_replaced():
    table = FakeTable()
    old = DynamoDbSessionService(table, ttl_seconds=-10)
    old._put_scoped_state("U#a#u", {"stale": 1}, time.time())
    svc = DynamoDbSessionService(table)
    assert await svc.get_user_state(app_name="a", user_id="u") == {}

    svc._put_scoped_state("U#a#u", {"fresh": 2}, time.time())

    assert await svc.get_user_state(app_name="a", user_id="u") == {"fresh": 2}


@pytest.mark.asyncio
async def test_non_ascii_session_ids_are_listed_and_deleted(svc):
    for sid in ("日本語", "~tilde", "plain"):
        await svc.create_session(app_name=APP, user_id="u", session_id=sid)

    listed = {s.id for s in (await svc.list_sessions(app_name=APP, user_id="u")).sessions}
    assert listed == {"日本語", "~tilde", "plain"}

    await svc.delete_session(app_name=APP, user_id="u", session_id="日本語")
    listed = {s.id for s in (await svc.list_sessions(app_name=APP, user_id="u")).sessions}
    assert listed == {"~tilde", "plain"}
