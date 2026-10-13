"""#672: an MCP connection logs what it negotiated, as one line.

`apply_patches()` wraps `ClientSession.adopt`, which both the legacy `initialize` handshake
and the 2026-07-28 `server/discover` end in. ADK picks between them with its feature flag
`ADK_ENABLE_MCP_MODERN_PROTOCOL` (#629). A small mcp 2.x MCPServer runs in this test
(mcp-server/ is not used); it answers both.
"""
import asyncio
import logging
import socket
import threading
import time
from types import SimpleNamespace

import pytest
import uvicorn
from google.adk.tools.mcp_tool import McpToolset, StreamableHTTPConnectionParams
from mcp.client.session import ClientSession
from mcp.server.mcpserver import MCPServer

from dak_agent.mcp_headers import SESSION_KEY_HEADER, session_key_header
from dak_agent.patches import apply_patches

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
    caplog.set_level(logging.INFO, logger="dak_agent.patches")
    return caplog


# Only `user_id` and `session.id` are read by `session_key_header`.
CONTEXT = SimpleNamespace(user_id="alice", session=SimpleNamespace(id="secret-session-7"))


def _list_tools(url):
    toolset = McpToolset(connection_params=StreamableHTTPConnectionParams(url=url), header_provider=session_key_header)

    async def go():
        try:
            return [t.name for t in await toolset.get_tools(readonly_context=CONTEXT)]
        finally:
            await toolset.close()

    return asyncio.run(go())


def _connected_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("MCP connected: ")]


def test_legacy_logs_negotiation(server_url, connect_log, monkeypatch):
    monkeypatch.delenv("ADK_ENABLE_MCP_MODERN_PROTOCOL", raising=False)
    assert _list_tools(server_url) == ["echo"]
    [line] = _connected_lines(connect_log)
    assert line.startswith("MCP connected: protocol=2025-11-25 server=connect-log/")
    assert "tools" in line.split("capabilities=")[1].split(",")


def test_modern_logs_negotiation(server_url, connect_log, monkeypatch):
    monkeypatch.setenv("ADK_ENABLE_MCP_MODERN_PROTOCOL", "1")
    assert _list_tools(server_url) == ["echo"]
    [line] = _connected_lines(connect_log)
    assert line.startswith("MCP connected: protocol=2026-07-28 server=connect-log/")
    assert "tools" in line.split("capabilities=")[1].split(",")


def test_no_credentials_in_log_or_new_headers(server_url, connect_log, monkeypatch):
    monkeypatch.delenv("ADK_ENABLE_MCP_MODERN_PROTOCOL", raising=False)
    _list_tools(server_url)
    with_log = {name for headers in SEEN_HEADERS for name in headers}
    key = SEEN_HEADERS[0][SESSION_KEY_HEADER.lower()]
    assert "secret-session-7" in key
    assert all(key not in r.getMessage() and "secret-session-7" not in r.getMessage() for r in connect_log.records)

    SEEN_HEADERS.clear()
    monkeypatch.setattr(ClientSession, "adopt", ClientSession.adopt.__wrapped__)
    _list_tools(server_url)
    assert with_log == {name for headers in SEEN_HEADERS for name in headers}
