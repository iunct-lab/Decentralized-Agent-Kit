"""Headers the agent puts on its MCP calls.

`X-DAK-Session-Key` names the user session a tool call belongs to. Its value
comes from ADK's `user_id` and `session_id`, so every call of one user session
carries the same value and calls of different sessions carry different ones.
It is not the MCP transport's `Mcp-Session-Id`, which the MCP server assigns
per connection (ADK's `mcp_session_manager.py` reads it from the response
headers) and which says nothing about which user session made the call.

ADK pools MCP connections by their headers, so a different session key also
means a different pooled connection.
"""
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from google.adk.agents.readonly_context import ReadonlyContext

SESSION_KEY_HEADER = "X-DAK-Session-Key"


def session_key_header(context: "ReadonlyContext") -> dict[str, str]:
    """`header_provider` for an ADK `McpToolset`."""
    return {SESSION_KEY_HEADER: f"{context.user_id}:{context.session.id}"}
