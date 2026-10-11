"""#633: a tool call's trace ID reaches the MCP server in ``_meta.traceparent``.

The traceparent is put there by the mcp SDK (SEP-414); DAK only makes sure a TracerProvider
exists (``ensure_tracer_provider``) so spans have real IDs without LangFuse, and logs the trace
ID of each allowed tool call. A small mcp 2.x MCPServer runs in this test (mcp-server/ is not used).
"""
import asyncio
import logging
import os
import socket
import subprocess
import sys
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
from mcp.server.mcpserver import Context, MCPServer
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from dak_agent.patches import ensure_tracer_provider
from dak_agent.permission import DEFAULT_RULES, PermissionPlugin, Rule

SEEN_META: list = []


def _echo_server() -> MCPServer:
    mcp = MCPServer("echo")

    @mcp.tool()
    async def echo(text: str, ctx: Context) -> str:
        SEEN_META.append(dict(ctx.request_context.meta or {}))
        return text

    return mcp


@pytest.fixture
def server_url():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(
        _echo_server().streamable_http_app(json_response=True), host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "echo server did not start"
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def spans():
    ensure_tracer_provider()
    exporter = InMemorySpanExporter()
    trace.get_tracer_provider().add_span_processor(SimpleSpanProcessor(exporter))
    SEEN_META.clear()
    yield exporter
    exporter.shutdown()


class ScriptedLlm(BaseLlm):
    calls: int = 0

    async def generate_content_async(self, llm_request, stream=False):
        self.calls += 1
        if self.calls == 1:
            part = types.Part(function_call=types.FunctionCall(id="fc-1", name="echo", args={"text": "hi"}))
        else:
            part = types.Part(text="done")
        yield LlmResponse(content=types.Content(role="model", parts=[part]))


def _call_echo(url):
    toolset = McpToolset(connection_params=StreamableHTTPConnectionParams(url=url))
    agent = LlmAgent(model=ScriptedLlm(model="scripted"), name="trace_agent", instruction="x", tools=[toolset])
    sessions = InMemorySessionService()
    # The default rules ask before an unknown tool of the default server; allow echo.
    plugin = PermissionPlugin(DEFAULT_RULES + [Rule("default", "echo", "*", "allow")], default_mcp_url=url)
    runner = Runner(app_name="trace", agent=agent, session_service=sessions, plugins=[plugin])

    async def go():
        session = await sessions.create_session(app_name="trace", user_id="u")
        message = types.Content(role="user", parts=[types.Part(text="echo")])
        try:
            return [e async for e in runner.run_async(user_id="u", session_id=session.id, new_message=message)]
        finally:
            await toolset.close()

    return asyncio.run(go())


def _traceparent_trace_id():
    [meta] = SEEN_META
    version, trace_id, span_id, flags = meta["traceparent"].split("-")
    return trace_id


def _tool_span_trace_id(exporter):
    [span] = [s for s in exporter.get_finished_spans() if s.name == "execute_tool echo"]
    return format(span.context.trace_id, "032x")


def test_traceparent_matches_tool_span(server_url, spans):
    events = _call_echo(server_url)
    assert any(fr.name == "echo" for e in events for fr in e.get_function_responses())
    trace_id = _traceparent_trace_id()
    assert trace_id != "0" * 32
    assert trace_id == _tool_span_trace_id(spans)


def test_permission_logs_trace_id(server_url, spans, caplog):
    log = logging.getLogger("dak_agent.permission")
    log.addHandler(caplog.handler)  # the logger does not propagate to caplog's root handler
    try:
        _call_echo(server_url)
    finally:
        log.removeHandler(caplog.handler)
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("tool_call ")]
    assert lines == [f"tool_call name=echo trace_id={_traceparent_trace_id()}"]
    assert _traceparent_trace_id() == _tool_span_trace_id(spans)


def test_permission_line_reaches_stderr():
    """No logging is configured at startup (uvicorn sets up only its own loggers); the line
    still reaches stderr. Outside any span the trace ID is `-`."""
    out = subprocess.run(
        [sys.executable, "-c", "from dak_agent.permission import _log_tool_call; _log_tool_call('echo')"],
        capture_output=True, text=True, check=True)
    assert "tool_call name=echo trace_id=-" in out.stderr.splitlines()


def test_permission_line_once_with_root_logging():
    """An entrypoint that configures root logging does not get the line twice."""
    code = ("import logging; logging.basicConfig(level=logging.INFO, format='%(message)s'); "
            "from dak_agent.permission import _log_tool_call; _log_tool_call('echo')")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stderr.splitlines().count("tool_call name=echo trace_id=-") == 1


def test_setup_telemetry_installs_provider():
    env = {k: v for k, v in os.environ.items() if not k.startswith("LANGFUSE_")}
    code = ("from opentelemetry import trace; from opentelemetry.sdk.trace import TracerProvider; "
            "from dak_agent.patches import setup_telemetry; setup_telemetry(); "
            "print(isinstance(trace.get_tracer_provider(), TracerProvider))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, env=env)
    assert out.stdout.strip() == "True"


def test_existing_provider_is_kept(spans):
    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    ensure_tracer_provider()
    assert trace.get_tracer_provider() is provider
