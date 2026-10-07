"""PBI #136: `dak:tools` chooses the tools of a call. Drives a real ADK
`Runner` (same technique as `test_call_scoped_instruction.py`) and inspects
the tool declarations on the recorded `LlmRequest`s."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from google.genai import types

from dak_agent.skill_registry import SkillRegistry


@pytest.fixture(autouse=True)
def no_remote_mcp_discovery():
    with patch("dak_agent.remote_tools.discover_remote_tools", AsyncMock(return_value={})):
        yield


def _recording_llm():
    from google.adk.models.base_llm import BaseLlm
    from google.adk.models.llm_response import LlmResponse

    requests = []

    class RecordingLlm(BaseLlm):
        async def generate_content_async(self, llm_request, stream=False):
            requests.append(llm_request)
            yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text="ok")]))

    return RecordingLlm(model="recording"), requests


def _app(llm):
    from google.adk.apps import App
    from google.adk.tools import FunctionTool

    from dak_agent.adaptive_agent import AdaptiveAgent
    from dak_agent.builtin_tools import switch_mode

    agent = AdaptiveAgent(model=llm, name="dak_agent", instruction="Base.", tools=[FunctionTool(switch_mode)])
    agent.skill_registry = MagicMock(spec=SkillRegistry)
    agent.skill_registry.get_skill.return_value = None
    agent.skill_registry.list_skills.return_value = []
    return App(name="dak_agent", root_agent=agent)


async def _declared_tools(state_delta=None):
    from google.adk.artifacts import InMemoryArtifactService
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService

    llm, requests = _recording_llm()
    app = _app(llm)
    sessions = InMemorySessionService()
    session = await sessions.create_session(app_name="dak_agent", user_id="u")
    runner = Runner(app=app, session_service=sessions, artifact_service=InMemoryArtifactService())
    async for _ in runner.run_async(
        user_id="u", session_id=session.id,
        new_message=types.Content(role="user", parts=[types.Part(text="hi")]),
        state_delta=state_delta,
    ):
        pass
    config_tools = requests[0].config.tools or []
    declared = [d.name for tool in config_tools for d in (tool.function_declarations or [])]
    assert sorted(declared) == sorted(requests[0].tools_dict)  # both views agree
    return declared


@pytest.mark.asyncio
async def test_empty_call_tools_sends_no_tool_declarations():
    assert await _declared_tools({"dak:tools": []}) == []


@pytest.mark.asyncio
async def test_call_tools_subset_sends_only_the_named_tools():
    assert await _declared_tools({"dak:tools": ["switch_mode"]}) == ["switch_mode"]


@pytest.mark.asyncio
async def test_without_call_tools_the_built_in_tools_are_sent_as_before():
    declared = await _declared_tools()
    assert {"switch_mode", "list_skills", "enable_skill"} <= set(declared)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["switch_mode", 3, ["switch_mode", 1], {"other": 1}])
async def test_malformed_call_tools_is_refused_without_calling_the_llm(bad):
    """A caller that asked for a restriction must not silently get every tool."""
    import json

    from google.adk.artifacts import InMemoryArtifactService
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService

    llm, requests = _recording_llm()
    app = _app(llm)
    sessions = InMemorySessionService()
    session = await sessions.create_session(app_name="dak_agent", user_id="u")
    runner = Runner(app=app, session_service=sessions, artifact_service=InMemoryArtifactService())
    texts = []
    async for event in runner.run_async(
        user_id="u", session_id=session.id,
        new_message=types.Content(role="user", parts=[types.Part(text="hi")]),
        state_delta={"dak:tools": bad},
    ):
        texts += [p.text for p in (event.content.parts if event.content else []) if p.text]

    assert requests == []
    assert json.loads(texts[-1])["error"] == "invalid_tools"


CALLER_MCP = "http://caller-mcp:9000/mcp"


def test_call_tools_mcp_servers_replaces_default_toolset(monkeypatch):
    from google.adk.tools import FunctionTool

    from dak_agent.adaptive_agent import AdaptiveAgent
    from dak_agent.builtin_tools import switch_mode

    monkeypatch.setenv("DAK_ALLOWED_MCP_URLS", CALLER_MCP)
    default_mcp = MagicMock()
    type(default_mcp).__name__ = "McpToolset"
    agent = AdaptiveAgent(model="m", name="dak_agent", instruction="x", tools=[FunctionTool(switch_mode), default_mcp])

    state = {"dak_active_skills": ["anything"], "temp:dak_caller_mcp_tools": {CALLER_MCP: ["read_file", "write_file"]}}
    with patch("dak_agent.skill_tools.make_mcp_toolset", side_effect=lambda *a, **k: ("toolset", *a)) as make:
        tools = agent._resolve_session_tools(
            state, {"dak:tools": {"mcp_servers": [{"url": CALLER_MCP, "type": "http"}], "names": ["read_file", "nope"]}})

    assert tools == [("toolset", CALLER_MCP, "http", ["read_file"])]  # no built-ins, no default MCP, no unknown names
    make.assert_called_once()


@pytest.mark.asyncio
async def test_caller_mcp_not_allowed_is_refused_without_calling_the_llm(monkeypatch):
    import json

    from google.adk.artifacts import InMemoryArtifactService
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService

    monkeypatch.setenv("DAK_ALLOWED_MCP_URLS", CALLER_MCP)
    llm, requests = _recording_llm()
    app = _app(llm)
    sessions = InMemorySessionService()
    session = await sessions.create_session(app_name="dak_agent", user_id="u")
    runner = Runner(app=app, session_service=sessions, artifact_service=InMemoryArtifactService())

    texts = []
    async for event in runner.run_async(
        user_id="u", session_id=session.id,
        new_message=types.Content(role="user", parts=[types.Part(text="hi")]),
        state_delta={"dak:tools": {"mcp_servers": [{"url": "http://169.254.169.254/latest"}]}},
    ):
        texts += [p.text for p in (event.content.parts if event.content else []) if p.text]

    assert requests == []
    error = json.loads(texts[-1])
    assert error["error"] == "mcp_server_not_allowed"
    assert error["requested_urls"] == ["http://169.254.169.254/latest"]


@pytest.mark.asyncio
async def test_names_dict_form_is_the_same_as_a_list():
    assert await _declared_tools({"dak:tools": {"names": ["switch_mode"]}}) == ["switch_mode"]


def test_caller_mcp_with_empty_names_means_no_tools(monkeypatch):
    from dak_agent.adaptive_agent import AdaptiveAgent

    monkeypatch.setenv("DAK_ALLOWED_MCP_URLS", CALLER_MCP)
    agent = AdaptiveAgent(model="m", name="dak_agent", instruction="x", tools=[])
    with patch("dak_agent.skill_tools.make_mcp_toolset") as make:
        tools = agent._resolve_session_tools({}, {"dak:tools": {"mcp_servers": [{"url": CALLER_MCP}], "names": []}})
    assert tools == []
    make.assert_not_called()


def test_duplicate_caller_mcp_servers_are_used_once(monkeypatch):
    from dak_agent import call_config

    monkeypatch.setenv("DAK_ALLOWED_MCP_URLS", CALLER_MCP)
    servers, _ = call_config.resolve_caller_mcp_servers(
        {"dak:tools": {"mcp_servers": [{"url": CALLER_MCP}, {"url": CALLER_MCP, "type": "http"}]}})
    assert servers == [{"url": CALLER_MCP, "type": "http"}]


def test_caller_mcp_toolsets_do_not_follow_redirects(monkeypatch):
    """An allowed endpoint must not be able to redirect the agent to an
    internal address (the MCP SDK's client follows redirects by default)."""
    from dak_agent import skill_tools

    caller = skill_tools.make_mcp_toolset(CALLER_MCP, "http", None, follow_redirects=False)
    default = skill_tools.make_mcp_toolset(CALLER_MCP, "http", None)
    for conn_type in ("http", "sse"):
        params = skill_tools.make_mcp_toolset(CALLER_MCP, conn_type, None, follow_redirects=False)._connection_params
        assert params.httpx_client_factory().follow_redirects is False
    assert caller._connection_params.httpx_client_factory().follow_redirects is False
    assert default._connection_params.httpx_client_factory().follow_redirects is True


@pytest.mark.asyncio
async def test_caller_mcp_form_does_not_list_the_default_mcp_tools(monkeypatch):
    from dak_agent.adaptive_agent import AdaptiveAgent

    monkeypatch.setenv("DAK_ALLOWED_MCP_URLS", CALLER_MCP)
    agent = AdaptiveAgent(model="m", name="dak_agent", instruction="x", tools=[])
    context = MagicMock()
    context.state = {}
    context._invocation_context.agent = agent.model_copy()
    with patch("dak_agent.call_config.resolve_dak_settings",
               return_value={"dak:tools": {"mcp_servers": [{"url": CALLER_MCP}]}}), \
            patch.object(AdaptiveAgent, "ensure_remote_tools_loaded", AsyncMock()) as ensure:
        await agent._restore_session_config(context)
    ensure.assert_not_called()


def _probe_agent(monkeypatch, get_tools):
    from dak_agent.adaptive_agent import AdaptiveAgent

    monkeypatch.setenv("DAK_ALLOWED_MCP_URLS", CALLER_MCP)
    agent = AdaptiveAgent(model="m", name="dak_agent", instruction="Base.", tools=[])
    toolset = MagicMock()
    toolset.get_tools = get_tools
    agent._mcp_toolset_cache[(CALLER_MCP, "http", frozenset(), "no-redirects")] = toolset
    return agent


def _tool(name):
    t = MagicMock()
    t.name = name
    return t


@pytest.mark.asyncio
async def test_unreachable_mcp_server_yields_structured_error_and_empty_tools(monkeypatch):
    from google.adk.sessions.state import State

    agent = _probe_agent(monkeypatch, AsyncMock(side_effect=ConnectionError("connection refused")))
    call = {"dak:tools": {"mcp_servers": [{"url": CALLER_MCP}]}}
    state = State(value=dict(call), delta={})

    await agent._probe_caller_mcp_servers(state, [{"url": CALLER_MCP, "type": "http"}])

    assert state["dak:tools_error"] == [{"url": CALLER_MCP, "reason": "unreachable: connection refused"}]
    assert agent._resolve_session_tools(state, call) == []  # no fallback to our tools
    assert "Unavailable tools" in agent._resolve_session_instruction(state, {})
    assert CALLER_MCP in agent._resolve_session_instruction(state, {})


@pytest.mark.asyncio
async def test_reachable_mcp_server_records_its_tool_names_and_clears_an_old_error(monkeypatch):
    from google.adk.sessions.state import State

    agent = _probe_agent(monkeypatch, AsyncMock(return_value=[_tool("read_file"), _tool("grep")]))
    state = State(value={"dak:tools_error": [{"url": CALLER_MCP, "reason": "unreachable: x"}]}, delta={})

    await agent._probe_caller_mcp_servers(state, [{"url": CALLER_MCP, "type": "http"}])

    assert state["temp:dak_caller_mcp_tools"] == {CALLER_MCP: ["grep", "read_file"]}
    assert state.get("dak:tools_error") is None
    assert "Unavailable tools" not in agent._resolve_session_instruction(state, {})


@pytest.mark.asyncio
async def test_a_hanging_mcp_server_times_out(monkeypatch):
    import asyncio

    from google.adk.sessions.state import State

    async def hang():
        await asyncio.sleep(60)

    agent = _probe_agent(monkeypatch, hang)
    monkeypatch.setattr("dak_agent.adaptive_agent.CALLER_MCP_PROBE_TIMEOUT_S", 0.05)
    state = State(value={}, delta={})

    await agent._probe_caller_mcp_servers(state, [{"url": CALLER_MCP, "type": "http"}])

    assert state["dak:tools_error"][0]["reason"].startswith("unreachable")


@pytest.mark.asyncio
async def test_an_old_tools_error_is_cleared_when_the_call_names_no_mcp_servers(monkeypatch):
    from google.adk.sessions.state import State

    agent = _probe_agent(monkeypatch, AsyncMock(return_value=[]))
    context = MagicMock()
    context.state = State(value={"dak:tools_error": [{"url": CALLER_MCP, "reason": "unreachable: x"}]}, delta={})
    context._invocation_context.agent = agent.model_copy()
    with patch("dak_agent.call_config.resolve_dak_settings", return_value={"dak:tools": None}):
        await agent._restore_session_config(context)

    assert context.state.get("dak:tools_error") is None
    assert "Unavailable tools" not in agent._resolve_session_instruction(context.state, {})


@pytest.mark.asyncio
async def test_servers_are_probed_concurrently(monkeypatch):
    import asyncio
    import time

    from google.adk.sessions.state import State

    other = "http://caller-mcp-2:9000/mcp"
    monkeypatch.setenv("DAK_ALLOWED_MCP_URLS", f"{CALLER_MCP},{other}")
    agent = _probe_agent(monkeypatch, AsyncMock())

    async def slow():
        await asyncio.sleep(0.3)
        return [_tool("t")]

    for url in (CALLER_MCP, other):
        ts = MagicMock()
        ts.get_tools = slow
        agent._mcp_toolset_cache[(url, "http", frozenset(), "no-redirects")] = ts
    monkeypatch.setenv("DAK_ALLOWED_MCP_URLS", f"{CALLER_MCP},{other}")
    state = State(value={}, delta={})

    started = time.monotonic()
    await agent._probe_caller_mcp_servers(state, [{"url": CALLER_MCP, "type": "http"}, {"url": other, "type": "http"}])

    assert time.monotonic() - started < 0.55  # not 0.3 + 0.3
    assert set(state["temp:dak_caller_mcp_tools"]) == {CALLER_MCP, other}


@pytest.mark.asyncio
async def test_a_call_that_will_be_refused_does_not_probe(monkeypatch):
    from google.adk.sessions.state import State

    monkeypatch.delenv("DAK_ALLOWED_INSPECTION_URLS", raising=False)
    for refused, error in (({"dak:model": "not-allowed"}, "model_not_allowed"),
                           ({"dak:inspection": {"http": {"url": "http://caller/v"}}}, "inspection_url_not_allowed")):
        get_tools = AsyncMock(return_value=[])
        agent = _probe_agent(monkeypatch, get_tools)
        context = MagicMock()
        context.state = State(value={}, delta={})
        context._invocation_context.agent = agent.model_copy()
        with patch("dak_agent.call_config.resolve_dak_settings",
                   return_value={**refused, "dak:tools": {"mcp_servers": [{"url": CALLER_MCP}]}}):
            content = await agent._restore_session_config(context)

        assert error in content.parts[0].text
        get_tools.assert_not_called()


def test_a_malformed_tools_error_in_state_is_ignored(monkeypatch):
    from dak_agent.adaptive_agent import AdaptiveAgent

    agent = AdaptiveAgent(model="m", name="dak_agent", instruction="Base.", tools=[])
    for bad in ("x", [1], [{"url": 3}], {"url": "u"}):
        assert agent._resolve_session_instruction({"dak:tools_error": bad}, {}) == "Base."
