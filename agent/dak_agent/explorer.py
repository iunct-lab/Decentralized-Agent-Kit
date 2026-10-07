"""A read-only sub-agent for broad investigation, offered to the root agent as a tool.

ADK's `AgentTool` runs the sub-agent in its own session, so the files it reads
and the tool results it sees never enter the parent's session: the parent gets
only the sub-agent's final answer as the tool's result (#85).

The sub-agent gets only the default MCP server's read-only tools; it cannot
write files or run commands. It runs on the model the calling agent uses in
that call (a caller's `dak:model` too), so delegated reads go to that model.
"""
from google.adk.agents import LlmAgent
from google.adk.tools.agent_tool import AgentTool
from google.adk.tools.mcp_tool import McpToolset, StreamableHTTPConnectionParams

from .mcp_headers import session_key_header

EXPLORER_NAME = "dak_explorer"
READ_ONLY_TOOL_NAMES = ["read_file", "list_files", "search_files", "grep", "deep_think"]
EXPLORER_INSTRUCTION = (
    "You are a sub-agent that only investigates. You cannot change files or run commands. "
    "Read what you need, then answer with your conclusions only, briefly: "
    "do not paste file contents or raw tool output."
)
DELEGATION_INSTRUCTION = (
    f"Delegate broad investigation (scanning or summarising many files) to the `{EXPLORER_NAME}` tool; "
    "it returns only its conclusions."
)


class _ExplorerTool(AgentTool):
    async def run_async(self, *, args, tool_context):
        model = tool_context._invocation_context.agent.canonical_model
        explorer = AgentTool(agent=self.agent.clone(update={"model": model}))  # a copy per call: sessions run at once
        return await explorer.run_async(args=args, tool_context=tool_context)


def make_explorer_tool(mcp_url: str) -> AgentTool:
    """The read-only investigation sub-agent wrapped as a tool for the root agent."""
    toolset = McpToolset(
        connection_params=StreamableHTTPConnectionParams(url=mcp_url),
        header_provider=session_key_header,
        tool_filter=READ_ONLY_TOOL_NAMES,
    )
    explorer = LlmAgent(
        name=EXPLORER_NAME,
        description="Investigates files read-only and returns only its conclusions.",
        instruction=EXPLORER_INSTRUCTION,
        tools=[toolset],
    )
    return _ExplorerTool(agent=explorer)
