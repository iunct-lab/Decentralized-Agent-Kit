import unittest
from unittest.mock import MagicMock

from google.adk.models.llm_response import LlmResponse
from google.genai import types

from dak_agent.enforcer import enforcer_validator, PLAN_KEY, ALWAYS_ALLOWED

# Marker used by enforcer to indicate blocked response
ENFORCER_BLOCKED_MARKER = "[ENFORCER_BLOCKED]"


def make_context(state: dict = None):
    """Create a mock CallbackContext whose .state behaves like ADK session state."""
    context = MagicMock()
    context.state = state if state is not None else {}
    context.session = MagicMock()
    context.session.contents = []
    return context


class TestEnforcerValidator(unittest.TestCase):
    def setUp(self):
        self.callback_context = make_context()

    def test_allow_tool_call(self):
        response = LlmResponse(
            content=types.Content(
                parts=[
                    types.Part(function_call=types.FunctionCall(name="some_tool", args={}))
                ],
                role="model"
            )
        )

        result = enforcer_validator(response, self.callback_context)
        self.assertIsNone(result, "Should allow response with tool call when no plan is active")

    def test_switch_mode_allowed_in_plan(self):
        # Set a restrictive plan
        self.callback_context.state[PLAN_KEY] = ["read_file"]

        response = LlmResponse(
            content=types.Content(
                parts=[
                    types.Part(function_call=types.FunctionCall(
                        name="switch_mode", args={"reason": "test", "new_focus": "test"}))
                ],
                role="model"
            )
        )

        result = enforcer_validator(response, self.callback_context)
        self.assertIsNone(result, "Should allow switch_mode even if not explicitly in plan (core tool)")

    def test_skill_tools_always_allowed(self):
        """list_skills/enable_skill must never be blocked, or the agent deadlocks."""
        self.callback_context.state[PLAN_KEY] = ["read_file"]

        for tool_name in ("list_skills", "enable_skill"):
            response = LlmResponse(
                content=types.Content(
                    parts=[types.Part(function_call=types.FunctionCall(name=tool_name, args={}))],
                    role="model"
                )
            )
            result = enforcer_validator(response, self.callback_context)
            self.assertIsNone(result, f"{tool_name} should always be allowed")

    def test_block_text_only(self):
        response = LlmResponse(
            content=types.Content(
                parts=[types.Part(text="Hello world")],
                role="model"
            )
        )

        result = enforcer_validator(response, self.callback_context)
        self.assertIsNotNone(result, "Should block text-only response")
        self.assertTrue(result.content and result.content.parts, "Should have content")

        has_text_error = False
        for part in result.content.parts:
            if hasattr(part, 'text') and part.text and ENFORCER_BLOCKED_MARKER in part.text:
                has_text_error = True
                self.assertIn("Direct responses are not allowed", part.text)
                break

        self.assertTrue(has_text_error, "Should return text-based enforcement error with marker")

    def test_block_disallowed_tool(self):
        self.callback_context.state[PLAN_KEY] = ["read_file"]

        response = LlmResponse(
            content=types.Content(
                parts=[types.Part(function_call=types.FunctionCall(name="run_command", args={}))],
                role="model"
            )
        )

        result = enforcer_validator(response, self.callback_context)
        self.assertIsNotNone(result, "Should block tool not in plan")
        text = result.content.parts[0].text
        self.assertIn(ENFORCER_BLOCKED_MARKER, text)
        self.assertIn("run_command", text)

    def test_state_is_per_context(self):
        """A plan set in one session must not leak into another session.

        Regression test: the old implementation kept the plan in a
        process-global SessionState shared by every session.
        """
        context_a = make_context()
        context_b = make_context()

        # Session A sets a restrictive plan via planner
        planner_response = LlmResponse(
            content=types.Content(
                parts=[types.Part(function_call=types.FunctionCall(
                    name="planner",
                    args={"task_description": "t", "plan_steps": ["s"], "allowed_tools": ["read_file"]}))],
                role="model"
            )
        )
        self.assertIsNone(enforcer_validator(planner_response, context_a))
        self.assertEqual(context_a.state.get(PLAN_KEY), ["read_file"])

        # Session A: non-plan tool is blocked
        blocked_response = LlmResponse(
            content=types.Content(
                parts=[types.Part(function_call=types.FunctionCall(name="run_command", args={}))],
                role="model"
            )
        )
        self.assertIsNotNone(enforcer_validator(blocked_response, context_a))

        # Session B (fresh state): the same tool is NOT blocked
        self.assertIsNone(enforcer_validator(blocked_response, context_b))
        self.assertNotIn(PLAN_KEY, context_b.state)

    def test_always_allowed_contains_core_tools(self):
        for name in ("planner", "ask_question", "attempt_answer", "switch_mode",
                     "list_skills", "enable_skill", "transfer_to_agent", "read_tool_output", "plan_exit"):
            self.assertIn(name, ALWAYS_ALLOWED)


if __name__ == '__main__':
    unittest.main()
