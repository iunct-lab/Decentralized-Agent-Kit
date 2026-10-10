import os
import unittest
from unittest.mock import MagicMock, patch
from dak_agent.config import DEFAULT_MODEL_NAME
from dak_agent.mode_manager import ModeManager

class TestModeManager(unittest.TestCase):
    def setUp(self):
        # The context-window branch reads MODEL_CONTEXT_WINDOW, so a value
        # exported in the developer's shell (direnv) would otherwise decide the
        # outcome of the default-resolution tests below.
        env_patch = patch.dict(os.environ, {}, clear=False)
        env_patch.start()
        os.environ.pop("MODEL_CONTEXT_WINDOW", None)
        self.addCleanup(env_patch.stop)

        self.mode_manager = ModeManager(model_name=DEFAULT_MODEL_NAME)

        # Create mock tools
        self.tool_switch = MagicMock()
        self.tool_switch.name = "switch_mode"
        self.tool_planner = MagicMock()
        self.tool_planner.name = "planner"
        self.tool_read = MagicMock()
        self.tool_read.name = "read_file"

        self.available_tools = [self.tool_switch, self.tool_planner, self.tool_read]

    @patch("dak_agent.mode_manager.meta_llm.complete_json")
    def test_returns_selected_tool_names(self, mock_complete_json):
        """Test that generate_mode_config returns tool names as strings."""
        mock_complete_json.return_value = {
            "instruction": "Read file",
            "selected_tools": ["read_file", "deep_think"],
        }

        instruction, selected_tool_names, selected_skills = self.mode_manager.generate_mode_config(
            history_summary="test",
            available_tools=self.available_tools,
            available_skills=[],
        )

        # Verify returns list of strings (tool names)
        self.assertIn("read_file", selected_tool_names)
        self.assertIn("deep_think", selected_tool_names)
        self.assertEqual(instruction, "Read file")

    @patch("dak_agent.mode_manager.meta_llm.complete_json")
    def test_returns_empty_list_on_error(self, mock_complete_json):
        """Test that generate_mode_config returns empty list on LLM error."""
        mock_complete_json.side_effect = Exception("API Error")

        instruction, selected_tool_names, selected_skills = self.mode_manager.generate_mode_config(
            history_summary="test",
            available_tools=self.available_tools,
            available_skills=[],
        )

        # Should return empty list and default instruction
        self.assertEqual(selected_tool_names, [])
        self.assertEqual(instruction, "Continue with current task.")

    def test_litellm_prefix_resolves_context_window(self):
        """A LiteLLM-prefixed model name still resolves its context window size."""
        manager = ModeManager(model_name="gemini/gemini-2.5-flash")
        self.assertGreater(manager.max_context_tokens, ModeManager.MODEL_MAX_TOKENS["default"])

    def test_bedrock_claude_resolves_context_window(self):
        """Bedrock inference-profile IDs resolve via litellm's model map."""
        manager = ModeManager(model_name="bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0")
        self.assertEqual(manager.max_context_tokens, 200000)

    def test_bedrock_claude_large_window_not_underestimated(self):
        """1M-window Bedrock Claude must not be clamped to the 200K family guess."""
        manager = ModeManager(model_name="bedrock/us.anthropic.claude-sonnet-4-20250514-v1:0")
        self.assertEqual(manager.max_context_tokens, 1000000)

    @patch("litellm.get_model_info", return_value={"max_input_tokens": 1050000})
    def test_bedrock_gpt56_resolves_context_window(self, mock_info):
        # Verify propagation, without depending on the remotely updated model map.
        manager = ModeManager(model_name="bedrock/us.openai.gpt-5.6-luna")
        self.assertEqual(manager.max_context_tokens, 1050000)
        mock_info.assert_called_with("bedrock/us.openai.gpt-5.6-luna")

    def test_unknown_model_uses_default_context_window(self):
        """IDs litellm can't map (e.g. a llama-server alias) get the conservative default."""
        manager = ModeManager(model_name="openai/llamacpp")
        self.assertEqual(manager.max_context_tokens, ModeManager.MODEL_MAX_TOKENS["default"])

    @patch.dict(os.environ, {"MODEL_CONTEXT_WINDOW": "8192"})
    def test_explicit_context_window_for_self_hosted_server(self):
        manager = ModeManager(model_name="openai/llamacpp")
        self.assertEqual(manager.max_context_tokens, 8192)

    @patch.dict(os.environ, {"MODEL_CONTEXT_WINDOW": "not-a-number"})
    @patch("dak_agent.mode_manager.ModeManager._lookup_max_tokens", return_value=12345)
    def test_invalid_explicit_context_window_falls_back(self, mock_lookup):
        manager = ModeManager(model_name="openai/llamacpp")
        self.assertEqual(manager.max_context_tokens, 12345)
        mock_lookup.assert_called_once_with("openai/llamacpp")

    def test_model_without_map_entry_tokens_uses_default(self):
        """litellm entries with max_input_tokens=None fall back to the default."""
        manager = ModeManager(model_name="ollama_chat/llama3.1:8b")
        self.assertEqual(manager.max_context_tokens, ModeManager.MODEL_MAX_TOKENS["default"])

    def test_default_model_resolves_full_context_window(self):
        """DEFAULT_MODEL_NAME resolves its full window via litellm's map.

        If a default bump lands on a model the pinned litellm doesn't map yet,
        this fails: add a MODEL_MAX_TOKENS override until litellm catches up.
        """
        manager = ModeManager(model_name=DEFAULT_MODEL_NAME)
        self.assertGreaterEqual(manager.max_context_tokens, 1_000_000)
        self.assertNotEqual(manager.max_context_tokens, ModeManager.MODEL_MAX_TOKENS["default"])

    def test_gemini_3x_resolves_context_window_via_litellm(self):
        """Gemini 3.x IDs that litellm already maps resolve without an override entry."""
        manager = ModeManager(model_name="gemini/gemini-3.5-flash-lite")
        self.assertGreater(manager.max_context_tokens, ModeManager.MODEL_MAX_TOKENS["default"])

    def test_requested_focus_is_consumed_once(self):
        """A stale LLM-requested focus must not leak into later automatic switches."""
        state = {}
        self.mode_manager.request_switch(state, reason="need tools", new_focus="deploy the app")
        self.assertEqual(self.mode_manager.consume_requested_focus(state), "deploy the app")
        self.assertIsNone(self.mode_manager.consume_requested_focus(state))

    def test_first_turn_never_switches(self):
        state = {}
        self.assertFalse(self.mode_manager.should_switch(state))

    def test_switch_requested_after_first_turn(self):
        state = {}
        self.mode_manager.should_switch(state)  # consume the first turn
        self.mode_manager.request_switch(state, reason="r", new_focus="f")
        self.assertTrue(self.mode_manager.should_switch(state))
        # The request is consumed: asking again without a new request must not re-trigger.
        self.assertFalse(self.mode_manager.should_switch(state))

    def test_session_state_is_per_session(self):
        """A regression test for the process-wide-instance bug: the same
        ModeManager is shared by every session, so its first-turn/switch-request
        flags must live in each session's own `state`, not on `self`."""
        session_a_state = {}
        session_b_state = {}

        # Session A consumes its first turn and requests a switch.
        self.assertFalse(self.mode_manager.should_switch(session_a_state))
        self.mode_manager.request_switch(session_a_state, reason="r", new_focus="f")

        # Session B's own first turn must still be untouched by session A.
        self.assertFalse(self.mode_manager.should_switch(session_b_state))
        self.assertFalse(session_b_state.get("dak_mode_switch_requested", False))

        # Session A's switch request must still be pending, independent of B.
        self.assertTrue(self.mode_manager.should_switch(session_a_state))

if __name__ == '__main__':
    unittest.main()
