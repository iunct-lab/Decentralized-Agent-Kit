import pytest
import asyncio
from unittest.mock import MagicMock, patch
from dak_agent.adaptive_agent import AdaptiveAgent
from google.adk.tools import FunctionTool

# We need to test the interaction between AdaptiveAgent and a real/mocked MCP server
# Since spinning up a real server in unit tests is complex, we will mock the McpToolset response
# but ensure the AdaptiveAgent logic correctly handles it.

@pytest.mark.asyncio
async def test_list_skills_integration():
    """
    Test that list_skills correctly fetches tools from a mocked MCP connection.
    """
    # Mock the McpToolset to return a specific list of tools
    mock_tool1 = MagicMock()
    mock_tool1.name = "mock_tool_1"
    mock_tool1.description = "Description for mock tool 1"
    
    mock_tool2 = MagicMock()
    mock_tool2.name = "mock_tool_2"
    mock_tool2.description = "Description for mock tool 2"
    
    mock_tools_list = [mock_tool1, mock_tool2]
    
    # Patch McpToolset inside adaptive_agent.py
    with patch("dak_agent.remote_tools.McpToolset") as MockMcpToolset:
        # Configure the mock to return our tools
        mock_toolset_instance = MockMcpToolset.return_value
        
        # Create a future for the async result
        future = asyncio.Future()
        future.set_result(mock_tools_list)
        mock_toolset_instance.get_tools.return_value = future
        
        # Initialize agent
        agent = AdaptiveAgent(
            model="test-model",
            name="test_agent",
            instruction="instruction",
            tools=[],
            mcp_url="http://mock-mcp:8000"
        )
        
        # Find the list_skills tool
        list_skills_tool = next((t for t in agent.tools if t.name == "list_skills"), None)
        assert list_skills_tool is not None
        
        # Call list_skills
        result = await list_skills_tool.func()
        
        # Verify result
        assert "Individual Remote Tools" in result
        assert "- mock_tool_1: Description for mock tool 1" in result
        assert "- mock_tool_2: Description for mock tool 2" in result
        
        # Verify McpToolset was initialized with correct URL
        MockMcpToolset.assert_called()
        call_args = MockMcpToolset.call_args
        assert call_args.kwargs['connection_params'].url == "http://mock-mcp:8000"

@pytest.mark.asyncio
async def test_list_skills_empty():
    """Test handling of empty tool list."""
    with patch("dak_agent.remote_tools.McpToolset") as MockMcpToolset:
        mock_toolset_instance = MockMcpToolset.return_value
        
        future = asyncio.Future()
        future.set_result([])
        mock_toolset_instance.get_tools.return_value = future
        
        # Patch SkillRegistry in adaptive_agent module
        with patch("dak_agent.adaptive_agent.SkillRegistry") as MockRegistry:
            MockRegistry.return_value.list_skills.return_value = []
            
            agent = AdaptiveAgent(
                model="test-model",
                name="test_agent",
                instruction="instruction",
                tools=[],
                mcp_url="http://mock-mcp:8000"
            )
            
            # Find the list_skills tool
            list_skills_tool = next((t for t in agent.tools if t.name == "list_skills"), None)
            assert list_skills_tool is not None
            
            result = await list_skills_tool.func()
            assert "No skills or tools available." in result

@pytest.mark.asyncio
async def test_list_skills_error():
    """Test handling of connection error."""
    with patch("dak_agent.remote_tools.McpToolset") as MockMcpToolset:
        mock_toolset_instance = MockMcpToolset.return_value
        # get_tools is called in _ensure_remote_tools_loaded
        mock_toolset_instance.get_tools.side_effect = Exception("Connection refused")
        
        # Patch SkillRegistry in adaptive_agent module
        with patch("dak_agent.adaptive_agent.SkillRegistry") as MockRegistry:
            MockRegistry.return_value.list_skills.return_value = []
            
            agent = AdaptiveAgent(
                model="test-model",
                name="test_agent",
                instruction="instruction",
                tools=[],
                mcp_url="http://mock-mcp:8000"
            )
            
            # Find the list_skills tool
            list_skills_tool = next((t for t in agent.tools if t.name == "list_skills"), None)
            assert list_skills_tool is not None
            
            # Should not raise, but log warning and return empty/partial list
            result = await list_skills_tool.func()
            assert "No skills or tools available." in result
