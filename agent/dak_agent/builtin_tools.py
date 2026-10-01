"""Built-in agent control tools.

These tools are always available and are never removed by mode switching
or skill filtering.
"""
import json
import logging
import os
from typing import List, Optional

from google.adk.tools import FunctionTool

from . import plan_mode

logger = logging.getLogger(__name__)

# Session-state key holding the agent's plan: [{"step": str, "status": str}].
# Kept in state (not in the event history), so context compaction never
# summarises it away.
STATE_TODOS = "dak_todos"
TODO_STATUSES = ("pending", "in_progress", "done")
# The session's first user message, kept verbatim so compaction never loses it.
STATE_ORIGINAL_REQUEST = "dak_original_request"
# The agent's handoff for resuming after a context reset: {"objective": str,
# "done" / "decisions" / "next_steps" / "files" / "open_questions": [str]}.
STATE_HANDOFF = "dak_handoff"
HANDOFF_SECTIONS = (("done", "Done"), ("decisions", "Decisions"), ("next_steps", "Next steps"),
                    ("files", "Files"), ("open_questions", "Open questions"))


def attempt_answer(answer: str, confidence: str, sources_used: list[str], tool_context) -> str:
    """
    Provide a final answer to the user.
    Args:
        answer: The final answer to provide.
        confidence: Confidence level (e.g., "high", "medium", "low").
        sources_used: List of sources or tools used to derive the answer.
    """
    # End the invocation after providing the answer
    tool_context._invocation_context.end_invocation = True

    sources_str = ""
    if sources_used:
        sources_str = f"\n\nSources: {', '.join(sources_used)}"

    return f"Answer (Confidence: {confidence}):\n{answer}{sources_str}"


def ask_question(questions: list[str], context: str, tool_context) -> str:
    """
    Ask clarifying questions to the user.
    Args:
        questions: List of questions to ask.
        context: Why these questions are needed.
    """
    # End the invocation after asking questions
    tool_context._invocation_context.end_invocation = True

    questions_str = "\n".join([f"- {q}" for q in questions])
    return f"Context: {context}\n\nQuestions for user:\n{questions_str}\n\n(Waiting for user response...)"


def planner(task_description: str, plan_steps: list[str], allowed_tools: list[str] = [],
            enter_plan_mode: bool = False, tool_context=None) -> str:
    """
    Create a plan and restrict future actions to specific tools (Ulysses Pact).
    Args:
        task_description: Description of the task to plan for.
        plan_steps: Ordered list of steps to accomplish the task.
        allowed_tools: List of tool names you intend to use (e.g. ["read_file", "run_command"]).
                       'planner', 'ask_question', 'attempt_answer', 'switch_mode', 'write_todos',
                       'read_plan', 'read_original_request', 'write_handoff' and 'read_handoff' are always allowed.
        enter_plan_mode: True to only investigate and write the plan under plans/*.md before
                         changing anything (Plan mode).
    """
    plan_str = "\n".join([f"{i + 1}. {step}" for i, step in enumerate(plan_steps)])

    restriction_msg = ""
    if allowed_tools:
        restriction_msg = (
            f"\n\n[System] Ulysses Pact Active: You are now restricted to using only: "
            f"{', '.join(allowed_tools)}"
        )
    if enter_plan_mode and tool_context is not None:
        plan_mode.enter(tool_context.state)
        restriction_msg += (
            "\n\n[System] Plan Mode Active: only read-only tools are available. Mutating tools "
            "(write_file/edit_file outside plans/*.md, run_command) will be denied until plan_exit is approved."
        )

    return f"Plan recorded for '{task_description}':\n{plan_str}{restriction_msg}"


def _normalize_status(status) -> str:
    s = str(status or "").strip().lower().replace(" ", "_").replace("-", "_")
    s = {"completed": "done", "complete": "done", "inprogress": "in_progress"}.get(s, s)
    return s if s in TODO_STATUSES else "pending"


def _normalize_item(item) -> dict:
    if not isinstance(item, dict):
        return {"step": str(item), "status": "pending"}
    return {"step": str(item.get("step", "")), "status": _normalize_status(item.get("status"))}


def format_todos(items: list, max_chars: Optional[int] = None) -> str:
    """One numbered line per plan item: `1. [done] read repo`. Tolerates plan
    state not written by write_todos (a client may seed it).

    With `max_chars` (the plan injected into the instruction), a longer plan
    first loses its done steps (replaced by a count), then is cut at an item
    boundary with a pointer to read_plan. Numbers stay the original ones."""
    numbered = [(i + 1, _normalize_item(item)) for i, item in enumerate(items)]

    def render(rows) -> List[str]:
        return [f"{n}. [{item['status']}] {item['step']}" for n, item in rows]

    full = "\n".join(render(numbered))
    if max_chars is None or len(full) <= max_chars:
        return full

    done = sum(1 for _, item in numbered if item["status"] == "done")
    head = [f"({done} done steps omitted)"] if done else []
    rest = render([(n, item) for n, item in numbered if item["status"] != "done"])
    if len("\n".join(head + rest)) <= max_chars:
        return "\n".join(head + rest)

    def pointer(remaining: int) -> str:
        return f"... {remaining} more step{'' if remaining == 1 else 's'}. Call read_plan for the whole plan."

    shown = list(head)
    for i, line in enumerate(rest):
        tail = pointer(len(rest) - i - 1)
        if len("\n".join(shown + [line, tail])) <= max_chars:
            shown.append(line)
            continue
        if i == 0:
            # Keep the current (first open) step visible even if it alone is
            # too long: cut its text rather than dropping it.
            room = max_chars - len("\n".join(shown + ["", tail])) - 1
            if room > 0:
                shown.append(line[:room] + "…")
                i += 1
        return "\n".join(shown + [pointer(len(rest) - i)])[:max_chars]
    return "\n".join(shown)[:max_chars]


def write_todos(items: list[dict], tool_context) -> str:
    """
    Record (or replace) your plan and the progress of each step. Call it again
    whenever a step's status changes. The plan stays available after the
    conversation history is compacted.
    Args:
        items: The whole plan, in order, as a list. Each item is {"step": "...", "status": "pending" | "in_progress" | "done"}.
    """
    if isinstance(items, str):
        # Small models often send a nested array as a JSON string.
        try:
            items = json.loads(items)
        except ValueError:
            pass
    if not isinstance(items, list):
        # Never overwrite the saved plan with something that is not a plan.
        return ('Error: items must be a list like [{"step": "...", "status": "pending"}]; '
                "the saved plan was not changed.")
    todos = [_normalize_item(item) for item in items]
    tool_context.state[STATE_TODOS] = todos
    _refresh_instruction(tool_context)
    return f"Plan saved:\n{format_todos(todos)}"


def _refresh_instruction(tool_context) -> None:
    """Rebuild the running agent's instruction now, so the new plan is in the
    very next model call of this invocation (compaction happens inside long
    invocations). Done here rather than in an after_tool callback: ADK skips
    agent callbacks when a plugin (the context harness truncating a long
    output) returns a replacement. Same as `enable_skill` does for skills."""
    agent = getattr(getattr(tool_context, "_invocation_context", None), "agent", None)
    refresh = getattr(agent, "_apply_session_config", None)
    if callable(refresh):
        try:
            refresh(tool_context)
        except Exception as e:  # the plan is saved; it reaches the model from the next turn
            logger.error(f"Could not rebuild the instruction after saving to state: {e}", exc_info=True)


def read_plan(tool_context) -> str:
    """
    Read your current plan and the progress of each step (as saved by write_todos).
    """
    todos = tool_context.state.get(STATE_TODOS) or []
    return format_todos(todos) if isinstance(todos, list) and todos else "No plan recorded yet."


def read_original_request(tool_context) -> str:
    """
    Read the user's first request of this session in full (the `# Original Request`
    in your instructions may be cut short).
    """
    request = tool_context.state.get(STATE_ORIGINAL_REQUEST)
    return request if isinstance(request, str) and request else "No original request recorded yet."


def _as_list(value) -> List[str]:
    if isinstance(value, str):
        # Small models often send a nested array as a JSON string.
        try:
            parsed = json.loads(value)
        except ValueError:
            parsed = None
        if not isinstance(parsed, list):
            return [value] if value else []
        value = parsed
    if not isinstance(value, list):
        return [] if value is None else [str(value)]
    return [str(item) for item in value]


def format_handoff(handoff) -> str:
    """The handoff as text under six headings. Tolerates handoff state not
    written by write_handoff (a client may seed it)."""
    if not isinstance(handoff, dict) or not handoff:
        return "No handoff recorded yet."
    objective = str(handoff.get("objective") or "") or "(none)"
    lines = [f"Objective: {objective}"]
    for key, heading in HANDOFF_SECTIONS:
        items = _as_list(handoff.get(key))
        lines.append(f"{heading}:")
        lines.extend(f"- {item}" for item in items or ["(none)"])
    return "\n".join(lines)


def write_handoff(objective: str, done: list[str], decisions: list[str], next_steps: list[str],
                  files: list[str], open_questions: list[str], tool_context) -> str:
    """
    Record (or replace) your handoff: what someone resuming this task from
    scratch needs to go on without redoing finished work. Call it at
    milestones of a long task. It stays available after the conversation
    history is compacted or reset.
    Args:
        objective: What the task is for, in one or two sentences.
        done: What has been finished (and verified).
        decisions: Decisions made, with the reason when it matters.
        next_steps: What to do next, in order.
        files: Files (or other resources) that matter for the next steps.
        open_questions: What is still unclear or waiting on someone.
    """
    handoff = {
        "objective": str(objective or ""),
        "done": _as_list(done),
        "decisions": _as_list(decisions),
        "next_steps": _as_list(next_steps),
        "files": _as_list(files),
        "open_questions": _as_list(open_questions),
    }
    tool_context.state[STATE_HANDOFF] = handoff
    _refresh_instruction(tool_context)
    return f"Handoff saved:\n{format_handoff(handoff)}"


def read_handoff(tool_context) -> str:
    """
    Read your saved handoff in full (the `# Handoff` in your instructions may
    be cut short).
    """
    return format_handoff(tool_context.state.get(STATE_HANDOFF))


async def switch_mode(tool_context, reason: str = "", new_focus: str = "") -> str:
    """
    Request a mode switch.

    Args:
        reason: Why you want to switch modes (e.g., "Need to use File System tools").
        new_focus: What the new mode should focus on (e.g., "File Operations").

    Workflow:
    1. If you don't know what tools are available, call `list_skills` first.
    2. Call `switch_mode(reason="...", new_focus="...")` to switch to a mode that includes the desired tools.
    """
    # The switch happens here, while the tool runs: only a call that the
    # permission plugin let through (allowed, or approved) switches (#410).
    agent = getattr(getattr(tool_context, "_invocation_context", None), "agent", None)
    apply_switch = getattr(agent, "apply_switch_request", None)
    if apply_switch is not None:
        await apply_switch(tool_context, reason, new_focus)
    return f"Mode switch requested: {reason}. New focus: {new_focus}"


def plan_exit(plan_path: str, tool_context) -> str:
    """
    Ask the user to approve your plan and leave Plan mode. Only after they approve can
    you change files and run commands; if they reject it you stay in Plan mode.
    Args:
        plan_path: The plan file you wrote under plans/ (e.g. "plans/refactor.md").
    """
    # Registered with require_confirmation=True: ADK runs this body only after
    # the user approved, so a rejection leaves the session in Plan mode.
    plan_mode.exit_(tool_context.state)
    return f"Plan approved. Exited plan mode; mutating tools are available again. Approved plan: {plan_path}"


def planner_requires_confirmation() -> bool:
    """Whether `planner` pauses for human approval (opt-in).

    `planner` has no side effects: it records a plan and *narrows* the agent's
    own future tool set (Ulysses Pact). Requiring confirmation therefore buys
    no safety, and it stalls every client that cannot answer a confirmation
    request mid-run (`/run`, A2A, CLI), where planning is the agent's first
    step. Set DAK_PLANNER_REQUIRE_CONFIRMATION=true to get the old behaviour.
    """
    return os.getenv("DAK_PLANNER_REQUIRE_CONFIRMATION", "false").lower() == "true"


def make_builtin_tools(enforcer_mode: bool = False) -> List[FunctionTool]:
    """Create the built-in control tools for the root agent.

    `attempt_answer` / `ask_question` are only useful in Enforcer Mode, where
    free-text responses are blocked.
    """
    tools = [
        FunctionTool(planner, require_confirmation=planner_requires_confirmation()),
        FunctionTool(switch_mode, require_confirmation=False),
        FunctionTool(write_todos, require_confirmation=False),
        FunctionTool(read_plan, require_confirmation=False),
        FunctionTool(read_original_request, require_confirmation=False),
        FunctionTool(write_handoff, require_confirmation=False),
        FunctionTool(read_handoff, require_confirmation=False),
        FunctionTool(plan_exit, require_confirmation=True),
    ]
    if enforcer_mode:
        tools.append(FunctionTool(attempt_answer, require_confirmation=False))
        tools.append(FunctionTool(ask_question, require_confirmation=False))
    return tools
