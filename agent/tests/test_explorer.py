"""The read-only investigation sub-agent (#85): what it may use, and what the parent sees."""
import pytest
from google.genai import types

from dak_agent.explorer import EXPLORER_NAME, READ_ONLY_TOOL_NAMES, make_explorer_tool

RAW_MARKER = "RAW-FILE-CONTENT-7f3a"
CONCLUSION = "Conclusion: the repository has one service."


def test_make_explorer_tool_only_has_read_only_tools():
    tool = make_explorer_tool("unused-model", "http://mcp.invalid/mcp")
    assert tool.name == EXPLORER_NAME
    (toolset,) = tool.agent.tools
    assert toolset.tool_filter == READ_ONLY_TOOL_NAMES
    assert not {"write_file", "edit_file", "run_command"} & set(toolset.tool_filter)


def _request_text(llm_request) -> str:
    """Every text, call and result in a model request, as one string."""
    out = []
    for content in llm_request.contents:
        for part in content.parts or []:
            out.append(part.text or "")
            if part.function_call:
                out.append(str(part.function_call.args))
            if part.function_response:
                out.append(str(part.function_response.response))
    return "\n".join(out)


def _scripted_llm(first_call: types.FunctionCall, answer: str):
    """Calls `first_call` once, then answers `answer` once the tool result is back."""
    from google.adk.models.base_llm import BaseLlm
    from google.adk.models.llm_response import LlmResponse

    class ScriptedLlm(BaseLlm):
        requests: list = []

        async def generate_content_async(self, llm_request, stream=False):
            self.requests.append(_request_text(llm_request))
            answered = any(p.function_response for c in llm_request.contents for p in c.parts or [])
            part = types.Part(text=answer) if answered else types.Part(function_call=first_call)
            yield LlmResponse(content=types.Content(role="model", parts=[part]))

    return ScriptedLlm(model="scripted", requests=[])


@pytest.mark.asyncio
async def test_explorer_tool_hides_its_raw_tool_output_from_the_parent_request():
    from google.adk.agents import LlmAgent
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.adk.tools import FunctionTool

    def read_file(path: str) -> str:
        """Stands in for the MCP server's read_file."""
        return f"{path}: {RAW_MARKER}"

    explorer_llm = _scripted_llm(types.FunctionCall(name="read_file", args={"path": "README.md"}), CONCLUSION)
    explorer_tool = make_explorer_tool(explorer_llm, "http://mcp.invalid/mcp")
    explorer_tool.agent.tools = [FunctionTool(read_file)]  # the real agent and AgentTool, no MCP server
    parent_llm = _scripted_llm(
        types.FunctionCall(name=EXPLORER_NAME, args={"request": "Summarise the repository."}), "done")
    parent = LlmAgent(name="dak_agent", model=parent_llm, instruction="Answer.", tools=[explorer_tool])

    sessions = InMemorySessionService()
    runner = Runner(app_name="dak_agent", agent=parent, session_service=sessions)
    session = await sessions.create_session(app_name="dak_agent", user_id="u")
    final = None
    async for event in runner.run_async(user_id="u", session_id=session.id, new_message=types.Content(
            role="user", parts=[types.Part(text="What is in the repository?")])):
        if event.is_final_response():
            final = event.content.parts[0].text

    assert final == "done"
    assert any(RAW_MARKER in r for r in explorer_llm.requests)  # the sub-agent did read the file
    assert len(parent_llm.requests) == 2
    assert all(RAW_MARKER not in r for r in parent_llm.requests)
    assert CONCLUSION in parent_llm.requests[-1]
    stored = await sessions.get_session(app_name="dak_agent", user_id="u", session_id=session.id)
    assert all(RAW_MARKER not in str(e.content) for e in stored.events)
