import os
import shutil
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from google.adk.tools.mcp_tool import StreamableHTTPConnectionParams
from google.adk.tools.mcp_tool.mcp_session_manager import MCPSessionManager

from dak_agent.adaptive_agent import AdaptiveAgent
from dak_agent.config import load_agent_config
from dak_agent.mcp_headers import SESSION_KEY_HEADER, session_key_header


def _ctx(user_id: str, session_id: str):
    return SimpleNamespace(user_id=user_id, session=SimpleNamespace(id=session_id))


def test_session_key_header_differs_for_different_sessions():
    assert session_key_header(_ctx("u1", "s1")) != session_key_header(_ctx("u2", "s2"))


def test_session_key_header_differs_when_only_one_part_differs():
    assert session_key_header(_ctx("u1", "s1")) != session_key_header(_ctx("u1", "s2"))
    assert session_key_header(_ctx("u1", "s1")) != session_key_header(_ctx("u2", "s1"))


def test_session_key_header_does_not_collide_across_the_separator():
    assert session_key_header(_ctx("a:b", "c")) != session_key_header(_ctx("a", "b:c"))


def test_session_key_header_value_is_a_safe_header_value():
    value = session_key_header(_ctx("ユーザー\r\nX-Evil: 1", "s 1"))[SESSION_KEY_HEADER]
    assert value.isascii() and value.isprintable() and " " not in value


def test_session_key_header_is_stable_for_same_session():
    assert session_key_header(_ctx("u1", "s1")) == session_key_header(_ctx("u1", "s1"))


def test_session_key_header_uses_expected_key():
    assert list(session_key_header(_ctx("u1", "s1"))) == [SESSION_KEY_HEADER]


def test_session_key_differentiates_mcp_session_pool():
    # ADK's real session manager: only the pool key is computed, nothing connects.
    manager = MCPSessionManager(StreamableHTTPConnectionParams(url="http://example.invalid/mcp"))
    key_a = manager._session_key_for(session_key_header(_ctx("u1", "s1")))
    key_b = manager._session_key_for(session_key_header(_ctx("u2", "s2")))
    assert key_a != key_b
    assert key_a == manager._session_key_for(session_key_header(_ctx("u1", "s1")))


# --- which servers get the header (#696) ---
# The value holds the user id and session id, so it goes only to DAK's own
# mcp-server: the default one, and agent_config.yaml servers that set
# `send_session_key: true`. Never to a server the caller chose.

CONFIG = """\
mcp_servers:
  - name: "own"
    url: "http://own-mcp:8000/mcp"
    type: "http"
    send_session_key: true
  - name: "search"
    url: "https://search.example/mcp/"
    type: "http"
"""


@pytest.fixture
def agent():
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "agent_config.yaml"), "w") as f:
        f.write(CONFIG)
    config = load_agent_config(os.path.join(d, "agent_config.yaml"), env={})
    shutil.rmtree(d)
    with patch("dak_agent.adaptive_agent.load_agent_config", return_value=config), \
            patch("dak_agent.remote_tools.discover_remote_tools", AsyncMock(return_value={})):
        yield AdaptiveAgent(model="test-model", name="test_agent", instruction="x", tools=[])


def test_configured_server_without_send_session_key_gets_no_session_key(agent):
    toolset = agent._cached_mcp_toolset("https://search.example/mcp/", "http", {"search-web"}, server="search")
    assert toolset._header_provider is None


def test_configured_server_with_send_session_key_gets_the_session_key(agent):
    toolset = agent._cached_mcp_toolset("http://own-mcp:8000/mcp", "http", {"t"}, server="own")
    assert toolset._header_provider is session_key_header


def test_caller_server_never_gets_the_session_key(agent):
    # Same URL as the `send_session_key: true` server: the caller still gets none.
    toolset = agent._cached_mcp_toolset("http://own-mcp:8000/mcp", "http", {"t"}, follow_redirects=False)
    assert toolset._header_provider is None


def test_default_server_gets_the_session_key(agent):
    toolset = agent._cached_mcp_toolset(agent.mcp_url, "http", set())
    assert toolset._header_provider is session_key_header
