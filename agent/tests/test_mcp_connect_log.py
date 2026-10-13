"""#672: an MCP connection logs what it negotiated, as one line.

`apply_patches()` wraps `ClientSession.adopt`, which both the legacy `initialize` handshake
and the 2026-07-28 `server/discover` end in. ADK picks between them with its feature flag
`ADK_ENABLE_MCP_MODERN_PROTOCOL` (#629). A small mcp 2.x MCPServer runs in this test
(mcp-server/ is not used); it answers both.
"""
import asyncio
import logging
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest
import uvicorn
from google.adk.agents import LlmAgent
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools.mcp_tool import McpToolset, StreamableHTTPConnectionParams
from google.genai import types
from mcp.client.session import ClientSession
from mcp.server.mcpserver import MCPServer

from dak_agent.mcp_headers import SESSION_KEY_HEADER, session_key_header
from dak_agent.patches import apply_patches, log_mcp_negotiation, negotiation_logger
from dak_agent.permission import DEFAULT_RULES, PermissionPlugin, Rule

SEEN_HEADERS: list = []


def _connect_log_server() -> MCPServer:
    mcp = MCPServer("connect-log")

    @mcp.tool()
    async def echo(text: str) -> str:
        return text

    return mcp


def _recording(app, seen: list):
    """Pass every request on, keeping its headers."""
    async def wrapped(scope, receive, send):
        if scope["type"] == "http":
            seen.append({k.decode().lower(): v.decode() for k, v in scope["headers"]})
        await app(scope, receive, send)

    return wrapped


@pytest.fixture
def server_url():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    app = _recording(_connect_log_server().streamable_http_app(json_response=True), SEEN_HEADERS)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "connect-log server did not start"
        time.sleep(0.05)
    SEEN_HEADERS.clear()
    yield f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def connect_log(caplog):
    apply_patches()
    negotiation_logger.addHandler(caplog.handler)  # the logger does not propagate to caplog's root handler
    try:
        yield caplog
    finally:
        negotiation_logger.removeHandler(caplog.handler)


class ScriptedLlm(BaseLlm):
    """Calls `echo` once, then answers."""
    calls: int = 0

    async def generate_content_async(self, llm_request, stream=False):
        self.calls += 1
        if self.calls == 1:
            part = types.Part(function_call=types.FunctionCall(id="fc-1", name="echo", args={"text": "hi"}))
        else:
            part = types.Part(text="done")
        yield LlmResponse(content=types.Content(role="model", parts=[part]))


SESSION_ID = "secret-session-7"


def _call_echo(url):
    """Run one agent turn that calls `echo` through the MCP toolset; return what the tool answered."""
    toolset = McpToolset(connection_params=StreamableHTTPConnectionParams(url=url), header_provider=session_key_header)
    agent = LlmAgent(model=ScriptedLlm(model="scripted"), name="connect_log_agent", instruction="x", tools=[toolset])
    sessions = InMemorySessionService()
    plugin = PermissionPlugin(DEFAULT_RULES + [Rule("default", "echo", "*", "allow")], default_mcp_url=url)
    runner = Runner(app_name="connect-log", agent=agent, session_service=sessions, plugins=[plugin])

    async def go():
        await sessions.create_session(app_name="connect-log", user_id="alice", session_id=SESSION_ID)
        message = types.Content(role="user", parts=[types.Part(text="echo")])
        try:
            events = [e async for e in runner.run_async(user_id="alice", session_id=SESSION_ID, new_message=message)]
        finally:
            await toolset.close()
        return [fr.response for e in events for fr in e.get_function_responses() if fr.name == "echo"]

    return asyncio.run(go())


def _echoed(responses):
    [response] = responses
    return response["content"][0]["text"]


def _connected_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("MCP connected: ")]


def test_legacy_logs_negotiation(server_url, connect_log, monkeypatch):
    monkeypatch.delenv("ADK_ENABLE_MCP_MODERN_PROTOCOL", raising=False)
    assert _echoed(_call_echo(server_url)) == "hi"
    [line] = _connected_lines(connect_log)
    assert line.startswith("MCP connected: protocol=2025-11-25 server=connect-log/")
    assert "tools" in line.split("capabilities=")[1].split(",")


def test_modern_logs_negotiation(server_url, connect_log, monkeypatch):
    monkeypatch.setenv("ADK_ENABLE_MCP_MODERN_PROTOCOL", "1")
    assert _echoed(_call_echo(server_url)) == "hi"
    [line] = _connected_lines(connect_log)
    assert line.startswith("MCP connected: protocol=2026-07-28 server=connect-log/")
    assert "tools" in line.split("capabilities=")[1].split(",")


def test_no_credentials_in_log_or_new_headers(server_url, connect_log, monkeypatch):
    monkeypatch.delenv("ADK_ENABLE_MCP_MODERN_PROTOCOL", raising=False)
    assert _echoed(_call_echo(server_url)) == "hi"
    with_log = {name for headers in SEEN_HEADERS for name in headers}
    key = SEEN_HEADERS[0][SESSION_KEY_HEADER.lower()]
    assert SESSION_ID in key
    assert all(key not in r.getMessage() and SESSION_ID not in r.getMessage() for r in connect_log.records)

    SEEN_HEADERS.clear()
    monkeypatch.setattr(ClientSession, "adopt", ClientSession.adopt.__wrapped__)
    assert _echoed(_call_echo(server_url)) == "hi"
    assert with_log == {name for headers in SEEN_HEADERS for name in headers}


def test_line_reaches_stderr():
    """No logging is configured at startup (uvicorn sets up only its own loggers); the line
    still reaches stderr."""
    script = (
        "from types import SimpleNamespace as N\n"
        "from dak_agent.patches import log_mcp_negotiation\n"
        "log_mcp_negotiation(N(protocol_version='2025-11-25', server_info=None, server_capabilities=None))\n"
    )
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)
    assert "MCP connected: protocol=2025-11-25 server=unknown capabilities=none" in out.stderr.splitlines()


def test_server_fields_stay_on_one_line(connect_log):
    """Name and version come from the server: a newline in them must not start a forged line."""
    forged = SimpleNamespace(name="evil\nMCP connected: protocol=forged", version="1\r\x1b[2K")
    log_mcp_negotiation(SimpleNamespace(protocol_version="2025-11-25", server_info=forged, server_capabilities=None))
    [line] = _connected_lines(connect_log)
    assert line.isprintable()
    assert line == r"MCP connected: protocol=2025-11-25 server=evil\nMCP connected: protocol=forged/1\r\x1b[2K capabilities=none"
