"""The read-only investigation sub-agent (#85): what it may use, and what the parent sees."""
import pytest
from google.genai import types

from dak_agent.explorer import EXPLORER_INSTRUCTION, EXPLORER_NAME, READ_ONLY_TOOL_NAMES, make_explorer_tool

RAW_MARKER = "RAW-FILE-CONTENT-7f3a"
CONCLUSION = "Conclusion: the repository has one service."


def test_make_explorer_tool_only_has_read_only_tools():
    tool = make_explorer_tool("http://mcp.invalid/mcp")
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


def _scripted_llm():
    """One model for both agents, as in production (the explorer runs on the parent's model).
    As the parent it delegates once to the explorer, as the explorer it reads one file;
    each answers once its tool result is back. Records each request's text by role."""
    from google.adk.models.base_llm import BaseLlm
    from google.adk.models.llm_response import LlmResponse

    class ScriptedLlm(BaseLlm):
        requests: dict = {}

        async def generate_content_async(self, llm_request, stream=False):
            role = "explorer" if EXPLORER_INSTRUCTION in str(llm_request.config.system_instruction) else "parent"
            self.requests.setdefault(role, []).append(_request_text(llm_request))
            answered = any(p.function_response for c in llm_request.contents for p in c.parts or [])
            if role == "explorer":
                call = types.FunctionCall(name="read_file", args={"path": "README.md"})
                answer = CONCLUSION
            else:
                call = types.FunctionCall(name=EXPLORER_NAME, args={"request": "Summarise the repository."})
                answer = "done"
            part = types.Part(text=answer) if answered else types.Part(function_call=call)
            yield LlmResponse(content=types.Content(role="model", parts=[part]))

    return ScriptedLlm(model="scripted", requests={})


@pytest.mark.asyncio
async def test_explorer_tool_hides_its_raw_tool_output_from_the_parent_request():
    from google.adk.agents import LlmAgent
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.adk.tools import FunctionTool

    def read_file(path: str) -> str:
        """Stands in for the MCP server's read_file."""
        return f"{path}: {RAW_MARKER}"

    llm = _scripted_llm()
    explorer_tool = make_explorer_tool("http://mcp.invalid/mcp")
    explorer_tool.agent.tools = [FunctionTool(read_file)]  # the real agent and AgentTool, no MCP server
    parent = LlmAgent(name="dak_agent", model=llm, instruction="Answer.", tools=[explorer_tool])

    sessions = InMemorySessionService()
    runner = Runner(app_name="dak_agent", agent=parent, session_service=sessions)
    session = await sessions.create_session(app_name="dak_agent", user_id="u")
    final = None
    async for event in runner.run_async(user_id="u", session_id=session.id, new_message=types.Content(
            role="user", parts=[types.Part(text="What is in the repository?")])):
        if event.is_final_response():
            final = event.content.parts[0].text

    assert final == "done"
    assert any(RAW_MARKER in r for r in llm.requests["explorer"])  # the sub-agent did read the file
    assert len(llm.requests["parent"]) == 2
    assert all(RAW_MARKER not in r for r in llm.requests["parent"])
    assert CONCLUSION in llm.requests["parent"][-1]
    stored = await sessions.get_session(app_name="dak_agent", user_id="u", session_id=session.id)
    assert all(RAW_MARKER not in str(e.content) for e in stored.events)


@pytest.mark.asyncio
async def test_explorer_runs_on_the_model_the_parent_uses_in_this_call():
    """A per-call model (a caller's `dak:model`) also carries the delegated reads (PR #528 review)."""
    from google.adk.agents import LlmAgent
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.adk.tools import FunctionTool

    def read_file(path: str) -> str:
        """Stands in for the MCP server's read_file."""
        return RAW_MARKER

    startup, chosen = _scripted_llm(), _scripted_llm()
    explorer_tool = make_explorer_tool("http://mcp.invalid/mcp")
    explorer_tool.agent.tools = [FunctionTool(read_file)]
    parent = LlmAgent(name="dak_agent", model=startup, instruction="Answer.", tools=[explorer_tool])

    async def choose_model(callback_context):
        callback_context._invocation_context.agent.model = chosen  # what AdaptiveAgent does for `dak:model`

    parent.before_agent_callback = choose_model
    sessions = InMemorySessionService()
    runner = Runner(app_name="dak_agent", agent=parent, session_service=sessions)
    session = await sessions.create_session(app_name="dak_agent", user_id="u")
    async for _ in runner.run_async(user_id="u", session_id=session.id, new_message=types.Content(
            role="user", parts=[types.Part(text="What is in the repository?")])):
        pass

    assert startup.requests == {}
    assert len(chosen.requests["explorer"]) == 2
