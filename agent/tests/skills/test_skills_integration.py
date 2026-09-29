import pytest
import os
import sys
from unittest.mock import MagicMock, patch

# Add agent directory to path
sys.path.append(os.path.join(os.path.dirname(__file__), "../.."))

from dak_agent.skill_registry import SkillRegistry
from google.adk.models import BaseLlm
from google.adk.models.llm_response import LlmResponse


# Mock McpToolset
class MockMcpToolset:
    def __init__(self, connection_params, tool_filter=None, require_confirmation=False, header_provider=None):
        self.tool_filter = tool_filter
        self.name = "MockMcpToolset"
        self._tools = []

    async def get_tools(self):
        # Return mock tools
        mock_tool = MagicMock()
        mock_tool.name = "remote_tool_1"
        mock_tool.description = "Description 1"
        return [mock_tool]


class MockModel(BaseLlm):
    model: str = "gemini-2.5-flash"

    def prompt(self, prompt: str, **kwargs) -> LlmResponse:
        return LlmResponse(content="mock response")

    async def generate_content_async(self, prompt: str, **kwargs) -> LlmResponse:
        return LlmResponse(content="mock response")


@pytest.fixture
def mock_agent():
    # Remote-tool discovery (list_skills) and skill toolsets (enable_skill)
    # both go through McpToolset, in remote_tools and skill_tools respectively.
    with patch('dak_agent.remote_tools.McpToolset', MockMcpToolset), \
         patch('dak_agent.skill_tools.McpToolset', MockMcpToolset):

        from dak_agent.adaptive_agent import AdaptiveAgent

        agent = AdaptiveAgent(
            model=MockModel(),
            name="TestAgent",
            instruction="System Prompt",
            tools=[],
            mcp_url="http://mock-mcp"
        )

        # Mock skill registry
        agent.skill_registry = MagicMock(spec=SkillRegistry)
        agent.skill_registry.skills_dirs = ["/tmp/mock_skills"]
        agent.skill_registry.find_skill_dir.side_effect = (
            lambda name: f"/tmp/mock_skills/{name}" if name == 'filesystem' else None
        )

        agent.skill_registry.list_skills.return_value = [
            {'name': 'filesystem', 'description': 'Manage files and directories'}
        ]
        agent.skill_registry.get_skill.side_effect = lambda name: {
            'name': 'filesystem',
            'description': 'Manage files and directories',
            'tools': ['read_file', 'list_files'],
            'instructions': 'Use this skill to manage files.'
        } if name == 'filesystem' else None

        yield agent


def get_tool_func(agent, tool_name):
    tool = next(t for t in agent.tools if getattr(t, 'name', '') == tool_name)
    return getattr(tool, 'fn', getattr(tool, 'func', None))


def make_tool_context(agent):
    """A ToolContext whose session `state` is a real dict and whose "live
    invocation copy" is `agent` itself, so `enable_skill`'s state writes are
    both real and immediately observable on `agent.instruction`/`agent.tools`
    (see AdaptiveAgent._apply_session_config/_live_agent)."""
    ctx = MagicMock()
    ctx.state = {}
    ctx._invocation_context.agent = agent
    return ctx


@pytest.mark.asyncio
async def test_list_skills(mock_agent):
    func = get_tool_func(mock_agent, 'list_skills')
    result = await func()

    assert "filesystem" in result
    assert "Manage files and directories" in result


@pytest.mark.asyncio
async def test_enable_skill(mock_agent):
    func = get_tool_func(mock_agent, 'enable_skill')

    # Skill has no local tools.py, so all tools fall back to MCP
    with patch('dak_agent.skill_tools.os.path.exists', return_value=False):
        result = await func(skill_name="filesystem", tool_context=make_tool_context(mock_agent))

    assert "'filesystem' enabled." in result
    assert "# Skill: filesystem" in mock_agent.instruction

    # Verify toolset added
    mcp_toolsets = [t for t in mock_agent.tools if getattr(t, 'name', '') == 'MockMcpToolset']
    assert len(mcp_toolsets) > 0
    assert set(mcp_toolsets[-1].tool_filter) == {'read_file', 'list_files'}


@pytest.mark.asyncio
async def test_enable_nonexistent_skill(mock_agent):
    func = get_tool_func(mock_agent, 'enable_skill')

    result = await func(skill_name="fake-skill", tool_context=make_tool_context(mock_agent))

    assert "Error" in result
    assert "not found" in result


@pytest.mark.asyncio
async def test_zero_config_discovery(mock_agent):
    # 1. List Skills (triggers lazy load)
    func_list = get_tool_func(mock_agent, 'list_skills')

    result = await func_list()

    # Verify remote tools are listed (MockMcpToolset returns remote_tool_1)
    assert "Individual Remote Tools" in result
    assert "remote_tool_1" in result

    # 2. Enable Remote Tool
    func_enable = get_tool_func(mock_agent, 'enable_skill')

    result = await func_enable(skill_name="remote_tool_1", tool_context=make_tool_context(mock_agent))

    assert "'remote_tool_1' enabled." in result
    assert "# Tool Enabled: remote_tool_1" in mock_agent.instruction

    # Verify toolset added with filter
    mcp_toolsets = [t for t in mock_agent.tools if getattr(t, 'name', '') == 'MockMcpToolset']
    assert mcp_toolsets[-1].tool_filter == ['remote_tool_1']
