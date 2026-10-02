"""Spike #328: can ADK's McpToolset answer a server's ``elicitation/create`` (old-spec MCP)?

A small mcp 1.x FastMCP server runs in this test (mcp-server/ is not used). Its ``confirm_probe``
asks for confirmation with ``ctx.elicit()`` and reports whether it would have run.
"""
import asyncio
import contextvars
import socket
import threading
import time

import pytest
import uvicorn
from google.adk.agents import LlmAgent
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools.mcp_tool import McpToolset, StreamableHTTPConnectionParams
from google.genai import types
from mcp.server.fastmcp import Context, FastMCP
from mcp.shared.context import RequestContext
from mcp.types import ElicitResult
from pydantic import BaseModel

QUESTION = "Run confirm_probe on /tmp/x?"


class ConfirmAnswer(BaseModel):
    proceed: bool


def _probe_server(json_response: bool) -> FastMCP:
    mcp = FastMCP("probe", json_response=json_response)

    @mcp.tool()
    async def confirm_probe(path: str, ctx: Context) -> str:
        answer = await ctx.elicit(QUESTION, schema=ConfirmAnswer)
        if answer.action == "accept" and answer.data.proceed:
            return "executed"
        return f"not executed: {answer.action}"

    return mcp


def _serve(json_response):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(
        _probe_server(json_response).streamable_http_app(), host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "probe server did not start"
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def server_url():
    yield from _serve(json_response=False)


@pytest.fixture
def json_response_url():
    """Like DAK's mcp-server: FastMCP(..., json_response=True)."""
    yield from _serve(json_response=True)


class ScriptedLlm(BaseLlm):
    requests: list = []

    async def generate_content_async(self, llm_request, stream=False):
        self.requests.append(llm_request)
        if len(self.requests) == 1:
            part = types.Part(function_call=types.FunctionCall(id="fc-1", name="confirm_probe", args={"path": "/tmp/x"}))
        else:
            part = types.Part(text="done")
        yield LlmResponse(content=types.Content(role="model", parts=[part]))


CALLER = contextvars.ContextVar("caller", default=None)


def _call_probe(url, callback, sse_read_timeout=30.0):
    """Run one agent turn whose model calls confirm_probe; return the tool's response."""
    toolset = McpToolset(
        connection_params=StreamableHTTPConnectionParams(url=url, sse_read_timeout=sse_read_timeout),
        elicitation_callback=callback,
    )

    def before_tool(tool, args, tool_context):
        CALLER.set(tool_context)  # visible to the callback only if it runs in the tool call's task

    agent = LlmAgent(model=ScriptedLlm(model="scripted", requests=[]), name="probe_agent", instruction="x",
                     tools=[toolset], before_tool_callback=before_tool)
    sessions = InMemorySessionService()
    runner = Runner(app_name="probe", agent=agent, session_service=sessions)

    async def go():
        session = await sessions.create_session(app_name="probe", user_id="u")
        message = types.Content(role="user", parts=[types.Part(text="probe")])
        try:
            events = [e async for e in runner.run_async(user_id="u", session_id=session.id, new_message=message)]
        finally:
            await toolset.close()
        return next(fr.response for e in events for fr in e.get_function_responses() if fr.name == "confirm_probe")

    return asyncio.run(go())


def _text(response):
    return "".join(c.get("text", "") for c in response.get("content", []))


def test_accept_runs_the_tool(server_url):
    seen = []

    async def accept(context, params):
        seen.append(params)
        return ElicitResult(action="accept", content={"proceed": True})

    response = _call_probe(server_url, accept)
    assert _text(response) == "executed"
    assert [p.message for p in seen] == [QUESTION]
    assert set(seen[0].requestedSchema["properties"]) == {"proceed"}


def test_decline_does_not_run_the_tool(server_url):
    async def decline(context, params):
        return ElicitResult(action="decline")

    assert _text(_call_probe(server_url, decline)) == "not executed: decline"


def test_callback_gets_no_tool_context(server_url):
    """The callback is per toolset, not per call: it gets the MCP request context, and the
    ADK ToolContext of the waiting call is not reachable from it (it runs in the session's
    receive loop, not in the tool call's task)."""
    seen = []

    async def record(context, params):
        seen.append((context, CALLER.get()))
        return ElicitResult(action="accept", content={"proceed": True})

    assert _text(_call_probe(server_url, record)) == "executed"
    [(context, caller)] = seen
    assert isinstance(context, RequestContext)
    assert caller is None


def test_open_call_is_bounded_by_sse_read_timeout(server_url):
    """While the callback waits (e.g. for a person), the tool call stays open; ADK's
    sse_read_timeout (default 300 s) ends it."""
    async def slow(context, params):
        await asyncio.sleep(3)
        return ElicitResult(action="accept", content={"proceed": True})

    response = _call_probe(server_url, slow, sse_read_timeout=1.0)
    assert "Timed out" in response["error"]


def test_json_response_server_cannot_elicit(json_response_url):
    """With json_response=True (as in mcp-server/main.py) the question never reaches the
    client: the POST answers with one JSON body, so there is no stream for elicitation/create.
    The call hangs until the read timeout."""
    seen = []

    async def accept(context, params):
        seen.append(params)
        return ElicitResult(action="accept", content={"proceed": True})

    response = _call_probe(json_response_url, accept, sse_read_timeout=2.0)
    assert seen == []
    assert "Timed out" in response["error"]
