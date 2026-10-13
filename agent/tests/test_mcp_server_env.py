"""MCP servers in agent_config.yaml that need environment variables (PBI #330).

A server with `requires_env` is dropped when any of those variables is unset or
blank; its skills are then hidden from `list_skills` and `enable_skill` refuses
them. `bearer_token_env` becomes an `Authorization: Bearer` header and
`url_env` replaces the URL. The key value must not reach a log.
"""
import logging
import os
import shutil
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dak_agent import skill_tools
from dak_agent.adaptive_agent import AdaptiveAgent
from dak_agent.config import load_agent_config

KEY = "xyzzy-plugh"  # not a real key; low entropy so secret scanners leave it alone

CONFIG = """\
mcp_servers:
  - name: "local-mcp"
    url: "http://mcp-server:8000/mcp"
    type: "http"
  - name: "search"
    url: "https://search.example/mcp/"
    type: "http"
    url_env: "SEARCH_MCP_URL"
    bearer_token_env: "SEARCH_API_KEY"
    requires_env: ["SEARCH_API_KEY"]
"""

SKILLS = {
    "web_lookup": "---\nname: web_lookup\ndescription: Look things up.\nmcp_server: search\ntools:\n  - search-web\n---\nSearch.\n",
    "ghost": "---\nname: ghost\ndescription: Points at a server nobody configured.\nmcp_server: nowhere\ntools:\n  - ghost-tool\n---\nBoo.\n",
}


@pytest.fixture
def workdir():
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "agent_config.yaml"), "w") as f:
        f.write(CONFIG)
    for name, text in SKILLS.items():
        os.makedirs(os.path.join(d, "skills", name))
        with open(os.path.join(d, "skills", name, "SKILL.md"), "w") as f:
            f.write(text)
    yield d
    shutil.rmtree(d)


@pytest.fixture(autouse=True)
def no_remote_mcp_discovery():
    with patch("dak_agent.remote_tools.discover_remote_tools", AsyncMock(return_value={})):
        yield


def _agent(workdir, env):
    config = load_agent_config(os.path.join(workdir, "agent_config.yaml"), env=env)
    with patch("dak_agent.adaptive_agent.load_agent_config", return_value=config):
        return AdaptiveAgent(model="test-model", name="test_agent", instruction="x", tools=[],
                             skills_dirs=[os.path.join(workdir, "skills")])


def _skill_tool(agent, name):
    return next(t for t in skill_tools.make_skill_tools(agent) if t.name == name).func


def _tool_context():
    context = MagicMock()
    context.state = {}
    return context


# --- unset: the server is dropped and nothing connects to it ---

@pytest.mark.parametrize("env", [{}, {"SEARCH_API_KEY": ""}, {"SEARCH_API_KEY": "  "}])
def test_unset_key_drops_the_server(workdir, env):
    config = load_agent_config(os.path.join(workdir, "agent_config.yaml"), env=env)
    assert set(config.mcp_servers) == {"local-mcp"}


@pytest.mark.asyncio
async def test_unset_key_hides_the_skill_from_list_skills(workdir):
    agent = _agent(workdir, {})
    listed = await _skill_tool(agent, "list_skills")()
    assert "web_lookup" not in listed


@pytest.mark.asyncio
async def test_unset_key_enable_skill_returns_the_reason(workdir):
    agent = _agent(workdir, {})
    context = _tool_context()
    with patch("dak_agent.call_config.resolve_dak_settings", return_value={}), \
            patch("dak_agent.skill_tools.make_mcp_toolset") as make:
        result = await _skill_tool(agent, "enable_skill")("web_lookup", tool_context=context)
    assert result.startswith("Error:") and "'search'" in result
    assert skill_tools.STATE_ACTIVE_SKILLS not in context.state
    make.assert_not_called()


def test_unset_key_active_skill_never_connects(workdir):
    """A session that enabled the skill earlier (when the key was set) must not
    fall back to the default mcp-server, nor try the dropped one."""
    agent = _agent(workdir, {})
    with patch("dak_agent.skill_tools.make_mcp_toolset") as make:
        agent._resolve_session_tools({skill_tools.STATE_ACTIVE_SKILLS: ["web_lookup", "ghost"]}, {})
    make.assert_not_called()


# --- set: the toolset carries the Bearer header, the key stays out of logs ---

def test_set_key_adds_bearer_header_and_url_env(workdir):
    env = {"SEARCH_API_KEY": KEY, "SEARCH_MCP_URL": "http://fake-search:8000/mcp"}
    config = load_agent_config(os.path.join(workdir, "agent_config.yaml"), env=env)
    server = config.mcp_servers["search"]
    assert server["url"] == "http://fake-search:8000/mcp"
    assert server["headers"] == {"Authorization": f"Bearer {KEY}"}
    assert "headers" not in config.mcp_servers["local-mcp"]


def test_set_key_without_url_env_keeps_the_configured_url(workdir):
    config = load_agent_config(os.path.join(workdir, "agent_config.yaml"), env={"SEARCH_API_KEY": KEY})
    assert config.mcp_servers["search"]["url"] == "https://search.example/mcp/"


@pytest.mark.asyncio
async def test_set_key_lists_and_enables_the_skill_with_the_header(workdir):
    agent = _agent(workdir, {"SEARCH_API_KEY": KEY})
    assert "web_lookup" in await _skill_tool(agent, "list_skills")()
    context = _tool_context()
    with patch("dak_agent.call_config.resolve_dak_settings", return_value={}), \
            patch.object(AdaptiveAgent, "_apply_session_config"):
        assert await _skill_tool(agent, "enable_skill")("web_lookup", tool_context=context) == "'web_lookup' enabled."
    with patch("dak_agent.skill_tools.make_mcp_toolset", return_value=MagicMock(name="toolset")) as make:
        agent._resolve_session_tools(context.state, {})
    make.assert_called_once_with("https://search.example/mcp/", "http", ["search-web"],
                                 headers={"Authorization": f"Bearer {KEY}"})
    assert all(KEY not in repr(k) for k in agent._mcp_toolset_cache)


def test_set_key_reaches_the_connection_params():
    with patch("dak_agent.skill_tools.McpToolset") as toolset:
        skill_tools.make_mcp_toolset("https://search.example/mcp/", "http", ["search-web"],
                                     headers={"Authorization": f"Bearer {KEY}"})
        skill_tools.make_mcp_toolset("https://search.example/sse", "sse", None,
                                     headers={"Authorization": f"Bearer {KEY}"})
    for call in toolset.call_args_list:
        params = call.kwargs["connection_params"]
        assert params.headers == {"Authorization": f"Bearer {KEY}"}
        assert KEY not in params.url


def test_set_key_value_is_not_logged(workdir, caplog):
    caplog.set_level(logging.DEBUG)
    agent = _agent(workdir, {"SEARCH_API_KEY": KEY})
    with patch("dak_agent.skill_tools.make_mcp_toolset", return_value=MagicMock(name="toolset")):
        agent._resolve_session_tools({skill_tools.STATE_ACTIVE_SKILLS: ["web_lookup"]}, {})
    assert caplog.records and KEY not in caplog.text


def test_set_key_toolset_is_not_shared_with_a_caller_server(workdir):
    """A caller-chosen server at the same URL must get its own toolset: no
    configured Bearer header, redirects refused, whatever the server is named."""
    with open(os.path.join(workdir, "agent_config.yaml"), "a") as f:
        f.write('  - name: "no-redirects"\n    url: "http://caller:9000/mcp"\n    type: "http"\n'
                '    bearer_token_env: "SEARCH_API_KEY"\n')
    agent = _agent(workdir, {"SEARCH_API_KEY": KEY})
    with patch("dak_agent.skill_tools.make_mcp_toolset", side_effect=lambda *a, **k: MagicMock()) as make:
        configured = agent._cached_mcp_toolset("http://caller:9000/mcp", "http", {"t"}, server="no-redirects")
        caller = agent._cached_mcp_toolset("http://caller:9000/mcp", "http", {"t"}, follow_redirects=False)
    assert configured is not caller
    assert make.call_args_list[1].kwargs == {"follow_redirects": False}
