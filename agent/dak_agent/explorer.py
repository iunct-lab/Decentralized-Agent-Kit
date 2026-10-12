"""A read-only sub-agent for broad investigation, offered to the root agent as a tool.

ADK's `AgentTool` runs the sub-agent in its own session, so the files it reads
and the tool results it sees never enter the parent's session: the parent gets
only the sub-agent's final answer as the tool's result (#85).

The sub-agent gets only the default MCP server's read-only tools; it cannot
write files or run commands. It runs on the model the calling agent uses in
that call (a caller's `dak:model` too), so delegated reads go to that model.
Its MCP calls carry the parent session's key, so in an isolated sandbox it
reads the parent's workspace, not the empty one of its own session (#527).
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


def _read_only_toolset(mcp_url: str, header_provider) -> McpToolset:
    return McpToolset(
        connection_params=StreamableHTTPConnectionParams(url=mcp_url),
        header_provider=header_provider,
        tool_filter=READ_ONLY_TOOL_NAMES,
    )


class _ExplorerTool(AgentTool):
    def __init__(self, *, agent, mcp_url: str):
        super().__init__(agent=agent)
        self._mcp_url = mcp_url

    async def run_async(self, *, args, tool_context):
        model = tool_context._invocation_context.agent.canonical_model
        # AgentTool runs the sub-agent in a new session; the key comes from the parent's
        # context only, not from session state the caller could write (#527)
        headers = session_key_header(tool_context)
        toolset = _read_only_toolset(self._mcp_url, lambda _ctx: headers)
        # a copy per call: sessions run at once, and the sub-runner closes only this call's toolset
        explorer = AgentTool(agent=self.agent.clone(update={"model": model, "tools": [toolset]}))
        return await explorer.run_async(args=args, tool_context=tool_context)


def make_explorer_tool(mcp_url: str) -> AgentTool:
    """The read-only investigation sub-agent wrapped as a tool for the root agent."""
    explorer = LlmAgent(
        name=EXPLORER_NAME,
        description="Investigates files read-only and returns only its conclusions.",
        instruction=EXPLORER_INSTRUCTION,
        tools=[_read_only_toolset(mcp_url, session_key_header)],
    )
    return _ExplorerTool(agent=explorer, mcp_url=mcp_url)
