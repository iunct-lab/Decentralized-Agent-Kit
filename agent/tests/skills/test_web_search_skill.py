"""The shipped web_search skill and its Tavily server in agent_config.yaml (#332).

The skill takes its tools from the `tavily` MCP server, which exists only when
TAVILY_API_KEY is set; the key itself is never written to a repository file.
"""
import os
import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dak_agent import skill_tools
from dak_agent.adaptive_agent import AdaptiveAgent
from dak_agent.config import load_agent_config
from dak_agent.skill_registry import SkillRegistry

AGENT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
REPO_ROOT = os.path.dirname(AGENT_DIR)
CONFIG = os.path.join(AGENT_DIR, "agent_config.yaml")
SKILLS_DIR = os.path.join(AGENT_DIR, "skills")
KEY = "xyzzy-plugh"  # not a real key; low entropy so secret scanners leave it alone


@pytest.fixture(autouse=True)
def no_remote_mcp_discovery():
    with patch("dak_agent.remote_tools.discover_remote_tools", AsyncMock(return_value={})):
        yield


def _agent(env):
    config = load_agent_config(CONFIG, env=env)
    with patch("dak_agent.adaptive_agent.load_agent_config", return_value=config):
        return AdaptiveAgent(model="test-model", name="test_agent", instruction="x", tools=[],
                             skills_dirs=[SKILLS_DIR])


def _list_skills(agent):
    return next(t for t in skill_tools.make_skill_tools(agent) if t.name == "list_skills").func()


def _read_repo_file(relpath):
    path = os.path.join(REPO_ROOT, relpath)
    if not os.path.exists(path):
        pytest.skip(f"{relpath} not available (not a repo checkout)")
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_web_search_skill_takes_its_tools_from_tavily():
    registry = SkillRegistry([SKILLS_DIR])
    registry.load_skills()
    skill = registry.skills["web_search"]
    assert skill["mcp_server"] == "tavily"
    assert skill["tools"] == ["tavily_search", "tavily_extract"]
    assert not os.path.exists(os.path.join(SKILLS_DIR, "web_search", "tools.py"))


def test_tavily_server_needs_the_key_and_sends_it_as_bearer():
    assert "tavily" not in load_agent_config(CONFIG, env={}).mcp_servers
    server = load_agent_config(CONFIG, env={"TAVILY_API_KEY": KEY}).mcp_servers["tavily"]
    assert server["url"] == "https://mcp.tavily.com/mcp/"
    assert server["headers"] == {"Authorization": f"Bearer {KEY}"}
    assert not server.get("send_session_key")
    assert KEY not in server["url"]
    url = load_agent_config(CONFIG, env={"TAVILY_API_KEY": KEY, "TAVILY_MCP_URL": "http://fake-tavily:8000/mcp"})
    assert url.mcp_servers["tavily"]["url"] == "http://fake-tavily:8000/mcp"


@pytest.mark.asyncio
async def test_web_search_listed_only_with_the_key():
    assert "web_search" not in await _list_skills(_agent({}))
    assert "web_search" in await _list_skills(_agent({"TAVILY_API_KEY": KEY}))


def test_enabled_web_search_connects_to_tavily_with_the_header():
    agent = _agent({"TAVILY_API_KEY": KEY})
    with patch("dak_agent.skill_tools.make_mcp_toolset", return_value=MagicMock(name="toolset")) as make:
        agent._resolve_session_tools({skill_tools.STATE_ACTIVE_SKILLS: ["web_search"]}, {})
    make.assert_called_once_with("https://mcp.tavily.com/mcp/", "http", ["tavily_extract", "tavily_search"],
                                 headers={"Authorization": f"Bearer {KEY}"})


def test_compose_passes_the_tavily_variables_through_bare():
    """Bare pass-through keeps an unset key unset in the container (not "")."""
    compose = _read_repo_file("docker-compose.yml")
    for name in ("TAVILY_API_KEY", "TAVILY_MCP_URL"):
        assert re.search(rf"(?m)^\s*-\s*{name}\s*$", compose)
        assert not re.search(rf"(?m)^\s*-?\s*{name}\s*[=:]", compose)


def test_env_example_leaves_the_key_empty():
    assert re.search(r"(?m)^TAVILY_API_KEY=$", _read_repo_file(".env.example"))
