import asyncio
import os
import unittest
from unittest.mock import MagicMock, patch

from dak_agent.builtin_tools import (
    ask_question,
    attempt_answer,
    STATE_TODOS,
    format_todos,
    make_builtin_tools,
    planner,
    read_plan,
    switch_mode,
    write_todos,
)


class TestBuiltinTools(unittest.TestCase):
    def test_make_builtin_tools_default(self):
        tools = make_builtin_tools(enforcer_mode=False)
        names = [t.name for t in tools]
        self.assertEqual(names, ["planner", "switch_mode", "write_todos", "read_plan"])

    def test_make_builtin_tools_enforcer(self):
        tools = make_builtin_tools(enforcer_mode=True)
        names = [t.name for t in tools]
        self.assertEqual(
            names, ["planner", "switch_mode", "write_todos", "read_plan", "attempt_answer", "ask_question"])

    def test_planner_does_not_block_on_confirmation_by_default(self):
        """A confirmation-gated planner stalls /run and A2A runs (no UI to approve)."""
        planner_tool = next(t for t in make_builtin_tools() if t.name == "planner")
        self.assertFalse(planner_tool._require_confirmation)

    @patch.dict(os.environ, {"DAK_PLANNER_REQUIRE_CONFIRMATION": "true"})
    def test_planner_confirmation_is_opt_in(self):
        planner_tool = next(t for t in make_builtin_tools() if t.name == "planner")
        self.assertTrue(planner_tool._require_confirmation)

    def test_planner_formats_plan(self):
        result = planner("My task", ["step one", "step two"], allowed_tools=["read_file"])
        self.assertIn("My task", result)
        self.assertIn("1. step one", result)
        self.assertIn("2. step two", result)
        self.assertIn("Ulysses Pact Active", result)
        self.assertIn("read_file", result)

    def test_planner_without_restriction(self):
        result = planner("My task", ["step one"])
        self.assertNotIn("Ulysses Pact", result)

    def test_switch_mode_message(self):
        tool_context = MagicMock()
        tool_context._invocation_context.agent = None  # no AdaptiveAgent: nothing to switch
        result = asyncio.run(switch_mode(tool_context, reason="too much context", new_focus="coding"))
        self.assertIn("too much context", result)
        self.assertIn("coding", result)

    def test_attempt_answer_ends_invocation(self):
        tool_context = MagicMock()
        result = attempt_answer("42", "high", ["deep_think"], tool_context)
        self.assertTrue(tool_context._invocation_context.end_invocation)
        self.assertIn("42", result)
        self.assertIn("high", result)
        self.assertIn("deep_think", result)

    def test_ask_question_ends_invocation(self):
        tool_context = MagicMock()
        result = ask_question(["What OS?"], "Need environment info", tool_context)
        self.assertTrue(tool_context._invocation_context.end_invocation)
        self.assertIn("What OS?", result)
        self.assertIn("Need environment info", result)


    def test_write_todos_persists_state_and_normalizes_status(self):
        tool_context = MagicMock()
        tool_context.state = {}

        result = write_todos(
            [{"step": "read repo", "status": "done"},
             {"step": "write summary", "status": "in_progress"},
             {"step": "review", "status": "blocked"},  # unknown -> pending
             {"step": "ship"}],                        # missing -> pending
            tool_context,
        )

        self.assertEqual(tool_context.state[STATE_TODOS], [
            {"step": "read repo", "status": "done"},
            {"step": "write summary", "status": "in_progress"},
            {"step": "review", "status": "pending"},
            {"step": "ship", "status": "pending"},
        ])
        self.assertIn("[done] read repo", result)
        self.assertIn("[pending] review", result)

    def test_read_plan_returns_state(self):
        tool_context = MagicMock()
        tool_context.state = {}
        self.assertEqual(read_plan(tool_context), "No plan recorded yet.")

        write_todos([{"step": "read repo", "status": "done"}], tool_context)
        self.assertEqual(read_plan(tool_context), "1. [done] read repo")

    def test_make_builtin_tools_includes_write_todos_and_read_plan(self):
        for enforcer_mode in (False, True):
            names = [t.name for t in make_builtin_tools(enforcer_mode=enforcer_mode)]
            self.assertIn("write_todos", names)
            self.assertIn("read_plan", names)

    def test_write_todos_and_read_plan_are_always_allowed_by_the_pact(self):
        from dak_agent.enforcer import ALWAYS_ALLOWED

        self.assertIn("write_todos", ALWAYS_ALLOWED)
        self.assertIn("read_plan", ALWAYS_ALLOWED)

    def test_write_todos_accepts_items_sent_as_a_json_string(self):
        """Small models often send a nested array as a JSON string."""
        tool_context = MagicMock()
        tool_context.state = {}

        write_todos('[{"step": "a", "status": "done"}]', tool_context)

        self.assertEqual(tool_context.state[STATE_TODOS], [{"step": "a", "status": "done"}])

    def test_write_todos_rejects_a_non_list_without_touching_the_saved_plan(self):
        tool_context = MagicMock()
        saved = [{"step": "keep me", "status": "in_progress"}]
        for bad in ({"step": "a", "status": "done"}, "not json", '{"step": "a"}', 3):
            tool_context.state = {STATE_TODOS: list(saved)}
            result = write_todos(bad, tool_context)
            self.assertTrue(result.startswith("Error:"), bad)
            self.assertEqual(tool_context.state[STATE_TODOS], saved, bad)

    def test_write_todos_normalizes_status_variants_and_plain_string_items(self):
        tool_context = MagicMock()
        tool_context.state = {}

        write_todos([{"step": "a", "status": "DONE"}, {"step": "b", "status": "completed"},
                     {"step": "c", "status": "in progress"}, "d"], tool_context)

        self.assertEqual([i["status"] for i in tool_context.state[STATE_TODOS]],
                         ["done", "done", "in_progress", "pending"])
        self.assertEqual(tool_context.state[STATE_TODOS][3]["step"], "d")

    def test_read_plan_tolerates_plan_state_not_written_by_write_todos(self):
        """A client may seed `dak_todos` when creating the session."""
        tool_context = MagicMock()
        tool_context.state = {STATE_TODOS: [{"step": "x"}, "y"]}
        self.assertEqual(read_plan(tool_context), "1. [pending] x\n2. [pending] y")

    def test_write_todos_keeps_the_plan_when_the_instruction_refresh_fails(self):
        tool_context = MagicMock()
        tool_context.state = {}
        tool_context._invocation_context.agent._apply_session_config.side_effect = RuntimeError("boom")

        result = write_todos([{"step": "a", "status": "done"}], tool_context)

        self.assertTrue(result.startswith("Plan saved"))
        self.assertEqual(tool_context.state[STATE_TODOS], [{"step": "a", "status": "done"}])

    def test_format_todos_drops_done_steps_first_when_over_the_limit(self):
        items = [{"step": f"old step {i}", "status": "done"} for i in range(30)] + [
            {"step": "write summary", "status": "in_progress"}, {"step": "review", "status": "pending"}]

        text = format_todos(items, max_chars=200)

        self.assertLessEqual(len(text), 200)
        self.assertTrue(text.startswith("(30 done steps omitted)"))
        self.assertIn("31. [in_progress] write summary", text)  # original numbering
        self.assertIn("32. [pending] review", text)

    def test_format_todos_cuts_at_item_boundary_and_points_to_read_plan(self):
        items = [{"step": f"step {i} " + "x" * 40, "status": "pending"} for i in range(50)]

        text = format_todos(items, max_chars=300)

        self.assertLessEqual(len(text), 300)
        self.assertTrue(text.startswith("1. [pending] step 0 "))
        shown = sum(1 for line in text.splitlines() if "[pending]" in line)
        self.assertTrue(text.endswith(f"... {50 - shown} more steps. Call read_plan for the whole plan."))
        self.assertTrue(all(line.endswith("x" * 40) for line in text.splitlines()[:-1]))  # no half items

    def test_format_todos_without_limit_or_under_it_is_unchanged(self):
        items = [{"step": "a", "status": "done"}, {"step": "b", "status": "pending"}]
        self.assertEqual(format_todos(items, max_chars=1_000), format_todos(items))
        self.assertEqual(format_todos(items), "1. [done] a\n2. [pending] b")

    def test_read_plan_is_never_truncated(self):
        tool_context = MagicMock()
        tool_context.state = {STATE_TODOS: [{"step": f"s{i} " + "y" * 200, "status": "pending"} for i in range(200)]}

        text = read_plan(tool_context)

        self.assertEqual(len(text.splitlines()), 200)
        self.assertNotIn("read_plan", text)

    def test_format_todos_keeps_the_first_open_step_even_if_it_alone_is_too_long(self):
        items = [{"step": f"old {i}", "status": "done"} for i in range(5)] + [
            {"step": "current " + "w" * 2000, "status": "in_progress"}, {"step": "next", "status": "pending"}]

        text = format_todos(items, max_chars=1000)

        self.assertLessEqual(len(text), 1000)
        self.assertIn("6. [in_progress] current www", text)
        self.assertTrue(text.endswith("... 1 more step. Call read_plan for the whole plan."))

    def test_format_todos_never_exceeds_a_tiny_limit(self):
        items = [{"step": "x" * 50, "status": "pending"} for _ in range(3)]
        self.assertLessEqual(len(format_todos(items, max_chars=20)), 20)


if __name__ == "__main__":
    unittest.main()
