import pytest
from unittest.mock import MagicMock

from google.adk.models.llm_response import LlmResponse
from google.genai import types

from dak_agent.enforcer import enforcer_validator, PLAN_KEY

# Marker used by enforcer to indicate blocked response
ENFORCER_BLOCKED_MARKER = "[ENFORCER_BLOCKED]"


@pytest.fixture
def mock_context():
    context = MagicMock()
    context.state = {}
    return context


def create_llm_response(tool_name=None, tool_args=None, text_content=None):
    parts = []
    if text_content:
        parts.append(types.Part(text=text_content))

    if tool_name:
        parts.append(types.Part(
            function_call=types.FunctionCall(
                name=tool_name,
                args=tool_args or {}
            )
        ))

    return LlmResponse(
        content=types.Content(parts=parts, role="model")
    )


def get_text_from_response(result):
    """Helper to extract text from enforcement error response."""
    if result and result.content and result.content.parts:
        for part in result.content.parts:
            if hasattr(part, 'text') and part.text:
                return part.text
    return ""


def test_direct_text_blocked(mock_context):
    """Test that direct text without tool calls is blocked."""
    response = create_llm_response(text_content="Hello world")
    result = enforcer_validator(response, mock_context)

    assert result is not None
    text = get_text_from_response(result)
    assert ENFORCER_BLOCKED_MARKER in text
    assert "Direct responses are not allowed" in text


def test_planner_sets_state(mock_context):
    """Test that calling planner stores the allowed tools in session state."""
    response = create_llm_response(
        tool_name="planner",
        tool_args={"allowed_tools": ["read_file", "run_command"]}
    )

    result = enforcer_validator(response, mock_context)

    assert result is None  # Should allow planner call
    assert mock_context.state[PLAN_KEY] == ["read_file", "run_command"]


def test_allowed_tool_passes(mock_context):
    """Test that using an allowed tool passes."""
    mock_context.state[PLAN_KEY] = ["read_file"]

    response = create_llm_response(tool_name="read_file", tool_args={"path": "test.txt"})
    result = enforcer_validator(response, mock_context)

    assert result is None


def test_disallowed_tool_blocked(mock_context):
    """Test that using a disallowed tool is blocked."""
    mock_context.state[PLAN_KEY] = ["read_file"]

    response = create_llm_response(tool_name="write_file", tool_args={"path": "test.txt", "content": "x"})
    result = enforcer_validator(response, mock_context)

    assert result is not None
    text = get_text_from_response(result)
    assert ENFORCER_BLOCKED_MARKER in text
    assert "Violation" in text
    assert "write_file" in text


def test_no_plan_allows_any_tool(mock_context):
    """Test that without a plan, any tool is allowed (except direct text)."""
    response = create_llm_response(tool_name="random_tool", tool_args={})
    result = enforcer_validator(response, mock_context)

    assert result is None


def test_planner_string_allowed_tools(mock_context):
    """An LLM may pass allowed_tools as a single string; it should be wrapped."""
    response = create_llm_response(
        tool_name="planner",
        tool_args={"allowed_tools": "read_file"}
    )

    result = enforcer_validator(response, mock_context)

    assert result is None
    assert mock_context.state[PLAN_KEY] == ["read_file"]


def test_replanning_updates_pact(mock_context):
    """Calling planner again replaces the previous pact."""
    mock_context.state[PLAN_KEY] = ["read_file"]

    response = create_llm_response(
        tool_name="planner",
        tool_args={"allowed_tools": ["write_file"]}
    )
    assert enforcer_validator(response, mock_context) is None
    assert mock_context.state[PLAN_KEY] == ["write_file"]

    # read_file is no longer allowed
    blocked = enforcer_validator(
        create_llm_response(tool_name="read_file"), mock_context
    )
    assert blocked is not None


def test_planner_restriction_does_not_block_write_todos_and_read_plan(mock_context):
    """PBI #87: the plan's progress (write_todos/read_plan, state `dak_todos`)
    is separate from the pact's tool restriction (planner, `enforcer_allowed_tools`)."""
    mock_context.state[PLAN_KEY] = ["read_file"]

    for tool_name, args in (("write_todos", {"items": [{"step": "a", "status": "done"}]}), ("read_plan", {}),
                            ("read_original_request", {})):
        assert enforcer_validator(create_llm_response(tool_name=tool_name, tool_args=args), mock_context) is None

    blocked = enforcer_validator(create_llm_response(tool_name="write_file", tool_args={"path": "x"}), mock_context)
    assert ENFORCER_BLOCKED_MARKER in get_text_from_response(blocked)
    assert mock_context.state[PLAN_KEY] == ["read_file"]  # recording progress does not touch the pact
