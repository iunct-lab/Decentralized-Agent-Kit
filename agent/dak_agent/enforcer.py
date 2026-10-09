"""Enforcer Mode (Ulysses Pact) response validator.

The active plan is stored in `callback_context.state`, which ADK persists
per session (and across restarts when a database session service is used).
This keeps plans from one session/user from leaking into another.
"""
import logging
from typing import List, Optional, Set

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_response import LlmResponse
from google.genai import types

logger = logging.getLogger(__name__)

# Session-state key holding the list of tools allowed by the active plan.
PLAN_KEY = "enforcer_allowed_tools"

# Core tools that are usable regardless of the active plan. Includes the
# client-side skill tools (list_skills/enable_skill) so the agent can always
# discover and enable capabilities, transfer_to_agent for A2A delegation, and
# read_tool_output so a truncated result of an allowed tool can be paged,
# get_context_remaining/new_context so the context usage can always be
# checked and the context reset from the handoff,
# write_todos/read_plan so the plan's progress can always be recorded and read,
# read_original_request so the whole request can always be read, and
# write_handoff/read_handoff so the handoff for a context reset can always be
# recorded and read, and plan_exit so a narrowed plan cannot keep the agent in
# Plan mode.
ALWAYS_ALLOWED: Set[str] = {
    "planner",
    "ask_question",
    "attempt_answer",
    "deep_think",
    "system_retry",
    "switch_mode",
    "list_skills",
    "enable_skill",
    "transfer_to_agent",
    "read_tool_output",
    "get_context_remaining",
    "new_context",
    "write_todos",
    "read_plan",
    "read_original_request",
    "write_handoff",
    "read_handoff",
    "plan_exit",
}

ENFORCER_INSTRUCTION = '''You are a helpful assistant powered by the Decentralized Agent Kit.

IMPORTANT: You are in ENFORCER MODE. You MUST use a tool for EVERY response. Direct text responses are NOT allowed.

# Ulysses Pact (Self-Correction)
You have the ability to bind yourself to a specific plan using the `planner` tool.
1. **Complex Tasks**: If a task requires multiple steps, call `planner` with `allowed_tools` list.
   - This creates a "Ulysses Pact" where the system will BLOCK any tool not in your plan.
   - This helps you stay focused and avoid hallucinations or off-track actions.
2. **Simple Tasks**: You can proceed without planning if the task is simple.

# Workflow
1. Analyze the request. Is it complex?
   - YES: Call `planner(..., allowed_tools=["tool_a", "tool_b"])`.
   - NO: Call the appropriate tool directly.
2. If you get a "Violation" error, it means you tried to use a tool not in your plan.
   - Fix: Use an allowed tool OR call `planner` again to update your plan.
3. **Context Management**: If you feel the conversation is getting too long or you are shifting to a completely new phase of the task:
   - Call `switch_mode(reason="...", new_focus="...")`.
   - This will refresh your context and tools for the new phase.

You can ONLY output text AFTER calling `ask_question` or `attempt_answer`. Otherwise you MUST call a tool.'''


def enforcer_validator(
    llm_response: LlmResponse,
    callback_context: CallbackContext,
) -> Optional[LlmResponse]:
    """
    Validates that the agent response follows the Ulysses Pact.
    1. Checks if 'planner' is called to update the pact.
    2. If a pact is active, enforces that only allowed tools are used.
    3. Blocks direct text responses.
    """
    tool_calls = []
    if llm_response.content and llm_response.content.parts:
        for part in llm_response.content.parts:
            if hasattr(part, "function_call") and part.function_call:
                tool_calls.append(part.function_call)

    # 1. Block direct text responses if no tool is called
    if not tool_calls:
        logger.info("Enforcer Mode: Blocked direct text response.")
        error_message = """
Direct responses are not allowed in Enforcer Mode.
You must use a tool for every step.

Available Tools:
- planner: Use this to plan and set your allowed tools (Ulysses Pact).
- ask_question: Use this to ask the user for clarification.
- attempt_answer: Use this to provide the FINAL answer.
- other tools: As defined in your plan.

You CANNOT just write text. You MUST call a tool.

If you are missing tools to fulfill the request:
1. Call `list_skills` to see available skills and tools.
2. Call `enable_skill(skill_name="...")` to enable what you need.
"""
        return _create_enforcement_error(error_message)

    # 2. Process tool calls
    for tool_call in tool_calls:
        tool_name = tool_call.name

        # planner updates the pact for this session
        if tool_name == "planner":
            args = tool_call.args or {}
            allowed_tools = args.get("allowed_tools") or []
            if isinstance(allowed_tools, str):
                allowed_tools = [allowed_tools]
            if allowed_tools:
                _set_plan(callback_context, list(allowed_tools))
            continue

        # 3. Enforce the pact if one is active
        allowed = _get_allowed_tools(callback_context)
        if allowed is not None and tool_name not in allowed:
            logger.info(f"Enforcer Mode: Blocked tool '{tool_name}' (not in plan).")
            error_message = f"""
🚫 Violation: Tool '{tool_name}' is not in your active plan.
Allowed tools: {sorted(allowed)}

Action:
1. Use an allowed tool.
2. OR call 'planner' again to update your plan and allowed tools.
"""
            return _create_enforcement_error(error_message)

    return None  # Allow response


def _set_plan(callback_context: CallbackContext, tools: List[str]) -> None:
    # Stored as a sorted list because session state must be JSON-serializable.
    callback_context.state[PLAN_KEY] = sorted(set(tools))
    logger.info(f"[Enforcer] Plan set! Allowed tools: {callback_context.state[PLAN_KEY]}")


def _get_allowed_tools(callback_context: CallbackContext) -> Optional[Set[str]]:
    """Return the allowed tool set for the active plan, or None if no plan is active."""
    plan = callback_context.state.get(PLAN_KEY)
    if not plan:
        return None
    return set(plan) | ALWAYS_ALLOWED


def _create_enforcement_error(error_message: str) -> LlmResponse:
    """
    Creates a text-based enforcement error response.

    The [ENFORCER_BLOCKED] marker allows clients (CLI/BFF) to detect
    these errors and automatically retry.
    """
    formatted_message = f"""[ENFORCER_BLOCKED]
{error_message}

---
[This response was blocked by Enforcer Mode. The model must use a tool to proceed.]
"""
    return LlmResponse(
        content=types.Content(
            parts=[types.Part(text=formatted_message)],
            role="model",
        ),
        turn_complete=True,
    )
