"""#629: with ADK_ENABLE_MCP_MODERN_PROTOCOL=1 (set in agent/Dockerfile) the agent speaks MCP 2026-07-28
(no session) to a server that does, and still reaches a server that only speaks the old handshake.

The servers run in this test (mcp-server/ is not used): a mcp 2.x MCPServer with one tool, ``echo``.
``legacy_server`` puts in front of it the answer mcp 1.30 gives to ``server/discover``.
"""
import asyncio
import json
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
from mcp.server.mcpserver import MCPServer

from dak_agent.mcp_headers import SESSION_KEY_HEADER, session_key_header

# What mcp 1.30 (mcp-server/ today, FastMCP with or without json_response) answers to a POST of
# server/discover without Mcp-Session-Id: it opens a session for the request, then rejects it because
# the request is not initialize and carries no session id (mcp/server/streamable_http.py,
# _validate_session). Read with `uv run --with mcp==1.30.0` against a FastMCP server on 2026-10-11.
LEGACY_DISCOVER_STATUS = 400
LEGACY_DISCOVER_BODY = {"jsonrpc": "2.0", "id": "server-error",
                        "error": {"code": -32600, "message": "Bad Request: Missing session ID"}}


def _echo_server() -> MCPServer:
    mcp = MCPServer("echo")

    @mcp.tool()
    def echo(text: str) -> str:
        return text

    return mcp


def _recording(app, seen: list, legacy: bool = False):
    """Records each POST's headers and JSON body. With ``legacy``, answers server/discover as mcp 1.30 does."""

    async def wrapped(scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST":
            return await app(scope, receive, send)
        chunks, more = [], True
        while more:
            message = await receive()
            chunks.append(message.get("body", b""))
            more = message.get("more_body", False)
        raw = b"".join(chunks)
        body = json.loads(raw) if raw else None
        seen.append(({k.decode().lower(): v.decode() for k, v in scope["headers"]}, body))
        if legacy and isinstance(body, dict) and body.get("method") == "server/discover":
            payload = json.dumps(LEGACY_DISCOVER_BODY).encode()
            await send({"type": "http.response.start", "status": LEGACY_DISCOVER_STATUS,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"mcp-session-id", b"0123456789abcdef0123456789abcdef")]})
            await send({"type": "http.response.body", "body": payload})
            return

        replayed = False

        async def replay():
            nonlocal replayed
            if replayed:
                return await receive()
            replayed = True
            return {"type": "http.request", "body": raw, "more_body": False}

        await app(scope, replay, send)

    return wrapped


def _serve(legacy: bool):
    seen: list = []
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    app = _recording(_echo_server().streamable_http_app(json_response=True), seen, legacy=legacy)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "echo server did not start"
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/mcp", seen
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def modern_server():
    yield from _serve(legacy=False)


@pytest.fixture
def legacy_server():
    yield from _serve(legacy=True)


class ScriptedLlm(BaseLlm):
    requests: list = []

    async def generate_content_async(self, llm_request, stream=False):
        self.requests.append(llm_request)
        if len(self.requests) == 1:
            part = types.Part(function_call=types.FunctionCall(id="fc-1", name="echo", args={"text": "hello"}))
        else:
            part = types.Part(text="done")
        yield LlmResponse(content=types.Content(role="model", parts=[part]))


def _call_echo(url):
    """Run one agent turn whose model calls echo through the toolset DAK builds; return the tool's response."""
    toolset = McpToolset(connection_params=StreamableHTTPConnectionParams(url=url), header_provider=session_key_header)
    agent = LlmAgent(model=ScriptedLlm(model="scripted", requests=[]), name="echo_agent", instruction="x",
                     tools=[toolset])
    sessions = InMemorySessionService()
    runner = Runner(app_name="echo", agent=agent, session_service=sessions)

    async def go():
        session = await sessions.create_session(app_name="echo", user_id="u")
        message = types.Content(role="user", parts=[types.Part(text="echo")])
        try:
            events = [e async for e in runner.run_async(user_id="u", session_id=session.id, new_message=message)]
        finally:
            await toolset.close()
        return next(fr.response for e in events for fr in e.get_function_responses() if fr.name == "echo")

    return asyncio.run(go())


def _text(response):
    return "".join(c.get("text", "") for c in response.get("content", []))


def _methods(seen):
    return [body.get("method") for _, body in seen if isinstance(body, dict)]


def test_modern_call_has_no_session(modern_server, monkeypatch):
    monkeypatch.setenv("ADK_ENABLE_MCP_MODERN_PROTOCOL", "1")
    url, seen = modern_server

    assert _text(_call_echo(url)) == "hello"
    methods = _methods(seen)
    assert "initialize" not in methods
    assert "server/discover" in methods
    assert all("mcp-session-id" not in headers for headers, _ in seen)
    [call] = [body for _, body in seen if isinstance(body, dict) and body.get("method") == "tools/call"]
    assert call["params"]["_meta"]["io.modelcontextprotocol/protocolVersion"] == "2026-07-28"
    assert all(SESSION_KEY_HEADER.lower() in headers for headers, _ in seen)


def test_legacy_server_falls_back(legacy_server, monkeypatch):
    monkeypatch.setenv("ADK_ENABLE_MCP_MODERN_PROTOCOL", "1")
    url, seen = legacy_server

    assert _text(_call_echo(url)) == "hello"
    methods = _methods(seen)
    assert methods.index("server/discover") < methods.index("initialize") < methods.index("tools/call")
