"""A read-only sub-agent for broad investigation, offered to the root agent as a tool.

ADK's `AgentTool` runs the sub-agent in its own session, so the files it reads
and the tool results it sees never enter the parent's session: the parent gets
only the sub-agent's final answer as the tool's result (#85).

The sub-agent gets only the default MCP server's read-only tools; it cannot
write files or run commands. It runs on the model the calling agent uses in
that call (a caller's `dak:model` too), so delegated reads go to that model.
Its MCP calls carry the parent session's key, so in an isolated sandbox it
reads the parent's workspace, not the empty one of its own session (#527).

A call the sub-agent must have confirmed (a `*.env` read by default) becomes a
pending approval of the `dak_explorer` call in the parent session. The
sub-agent's session is kept in the parent's session service until the answer
comes, then gets the same answer and runs on, so a restart in between does not
lose it (#535). A confirmation the sub-agent asks for after that is pending
in the parent session again.
"""
import asyncio
import uuid

from google.adk.agents import LlmAgent
from google.adk.agents._streaming_mode import StreamingMode
from google.adk.events import Event
from google.adk.memory.in_memory_memory_service import InMemoryMemoryService
from google.adk.runners import Runner
from google.adk.tools._forwarding_artifact_service import ForwardingArtifactService
from google.adk.tools.agent_tool import AgentTool
from google.adk.tools.mcp_tool import McpToolset, StreamableHTTPConnectionParams
from google.adk.utils.context_utils import Aclosing
from google.genai import types

from . import approvals
from .mcp_headers import session_key_header

EXPLORER_NAME = "dak_explorer"
EXPLORER_APP_NAME = "dak_explorer"
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
# What ADK's tools answer while a confirmation is pending (harness._CONFIRMATION_ANSWERS)
_PENDING_ANSWER = {"error": "This tool call requires confirmation, please approve or reject."}


def _read_only_toolset(mcp_url: str, header_provider) -> McpToolset:
    return McpToolset(
        connection_params=StreamableHTTPConnectionParams(url=mcp_url),
        header_provider=header_provider,
        tool_filter=READ_ONLY_TOOL_NAMES,
    )


def _child_session_id(tool_context) -> str:
    """The sub-agent's session for this call: the resumed call (same function call id) finds it again."""
    return f"{tool_context._invocation_context.session.id}:{tool_context.function_call_id}"


def _pending(session) -> list[dict]:
    return approvals.list_pending_approvals(
        [e.model_dump(mode="json", by_alias=True, exclude_none=True) for e in session.events])


def _confirmation_answers(session, confirmation) -> types.Content:
    """The parent's answer, given to each confirmation pending in the sub-agent's session."""
    return types.Content(role="user", parts=[
        types.Part(function_response=types.FunctionResponse(
            id=p["id"], name=approvals.REQUEST_CONFIRMATION,
            response={"confirmed": confirmation.confirmed, "payload": confirmation.payload}))
        for p in _pending(session)
    ])


async def _ask_again(ctx, tool_context, args) -> None:
    """ADK turns a requested confirmation into a pending approval only for a call it has not resumed.
    A resumed call (answered once, or approved before it ran) adds that event itself, in the shape and
    place ADK gives it: before the call's response, which carries the request (#535, PR #680 review)."""
    confirmation = tool_context.actions.requested_tool_confirmations[tool_context.function_call_id]
    request_id = f"adk-{uuid.uuid4()}"
    original = {"id": tool_context.function_call_id, "name": EXPLORER_NAME, "args": args}
    await ctx.session_service.append_event(ctx.session, Event(
        invocation_id=ctx.invocation_id, author=ctx.agent.name, branch=ctx.branch,
        content=types.Content(role="model", parts=[types.Part(function_call=types.FunctionCall(
            id=request_id, name=approvals.REQUEST_CONFIRMATION,
            args={"originalFunctionCall": original,
                  "toolConfirmation": confirmation.model_dump(by_alias=True, exclude_none=True)}))]),
        long_running_tool_ids={request_id},
    ))


class _ExplorerTool(AgentTool):
    def __init__(self, *, agent, mcp_url: str):
        super().__init__(agent=agent)
        self._mcp_url = mcp_url

    async def run_async(self, *, args, tool_context):
        ctx = tool_context._invocation_context
        model = ctx.agent.canonical_model
        # the sub-agent's session does not hold the key; it comes from the parent's context only,
        # not from session state the caller could write (#527)
        headers = session_key_header(tool_context)
        toolset = _read_only_toolset(self._mcp_url, lambda _ctx: headers)
        # a copy per call: sessions run at once, and the sub-runner closes only this call's toolset
        explorer = self.agent.clone(update={"model": model, "tools": [toolset]})
        sessions = ctx.session_service
        runner = Runner(
            app_name=EXPLORER_APP_NAME, agent=explorer, session_service=sessions,
            artifact_service=ForwardingArtifactService(tool_context), memory_service=InMemoryMemoryService(),
            credential_service=ctx.credential_service, plugins=ctx.plugin_manager.plugins,
        )
        runner.plugin_manager.set_skip_closing_plugins(True)  # the parent's plugins, still in use
        session_id = _child_session_id(tool_context)
        session = await sessions.get_session(app_name=EXPLORER_APP_NAME, user_id=ctx.user_id, session_id=session_id)
        if session is None:  # a new call, or one approved before it ran (a rule that asks for dak_explorer)
            await sessions.create_session(
                app_name=EXPLORER_APP_NAME, user_id=ctx.user_id, session_id=session_id,
                state={k: v for k, v in tool_context.state.to_dict().items() if not k.startswith("_adk")},
            )
            message = types.Content(role="user", parts=[types.Part.from_text(text=args["request"])])
        else:
            message = _confirmation_answers(session, tool_context.tool_confirmation)

        # as AgentTool: the caller's run settings, unary (only the last event is read), the caller's abort
        run_config = ctx.run_config
        if run_config is not None:
            run_config = run_config.model_copy(update={"support_cfc": False, "streaming_mode": StreamingMode.NONE})
        abort_signal = ctx._abort_signal if isinstance(ctx._abort_signal, asyncio.Event) else None
        last_content = last_error = None
        async with Aclosing(runner.run_async(user_id=ctx.user_id, session_id=session_id, new_message=message,
                                            run_config=run_config, abort_signal=abort_signal)) as events:
            async for event in events:
                if event.actions.state_delta:
                    tool_context.state.update(event.actions.state_delta)  # e.g. an `always` approval
                if event.error_message:
                    last_error = event.error_message
                if event.content:
                    last_content = event.content
        await runner.close()

        session = await sessions.get_session(app_name=EXPLORER_APP_NAME, user_id=ctx.user_id, session_id=session_id)
        pending = _pending(session)
        if pending:
            calls = [{"name": p["tool_name"], "args": p["tool_args"]} for p in pending]
            tool_context.request_confirmation(
                hint=f"{EXPLORER_NAME} wants to call " + "、".join(f"{c['name']}({c['args']})" for c in calls)
                     + ". Approving or rejecting applies to all of these calls.",
                payload={"explorer_calls": calls},
            )
            if tool_context.tool_confirmation is not None:
                await _ask_again(ctx, tool_context, args)
            return _PENDING_ANSWER
        await sessions.delete_session(app_name=EXPLORER_APP_NAME, user_id=ctx.user_id, session_id=session_id)
        text = "\n".join(p.text for p in (last_content.parts if last_content else None) or []
                         if p.text and not p.thought)
        return text or last_error or ""


def make_explorer_tool(mcp_url: str) -> AgentTool:
    """The read-only investigation sub-agent wrapped as a tool for the root agent."""
    explorer = LlmAgent(
        name=EXPLORER_NAME,
        description="Investigates files read-only and returns only its conclusions.",
        instruction=EXPLORER_INSTRUCTION,
        tools=[_read_only_toolset(mcp_url, session_key_header)],
    )
    return _ExplorerTool(agent=explorer, mcp_url=mcp_url)
