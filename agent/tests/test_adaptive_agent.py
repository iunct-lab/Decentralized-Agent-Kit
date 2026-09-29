import unittest
from unittest.mock import MagicMock, patch
import sys
import os

# Add parent directory to path to import modules
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dak_agent.adaptive_agent import AdaptiveAgent
from dak_agent.mode_manager import ModeManager, FIRST_TURN_DONE_KEY
from google.adk.tools import FunctionTool

class TestAdaptiveAgent(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.tool1 = MagicMock()
        self.tool1.name = "tool1"
        self.tool2 = MagicMock()
        self.tool2.name = "tool2"
        self.mock_tools = [self.tool1, self.tool2]

    def test_initialization(self):
        """Test that the agent initializes with correct tools."""
        # Add switch_mode to mock tools to simulate real usage
        mock_switch = MagicMock(spec=FunctionTool)
        mock_switch.name = "switch_mode"
        tools = self.mock_tools + [mock_switch]

        agent = AdaptiveAgent(
            model="test-model",
            name="test_agent",
            instruction="Initial instruction",
            tools=tools
        )

        tool_names = [t.name for t in agent.tools if hasattr(t, 'name')]
        self.assertIn("switch_mode", tool_names)
        self.assertIn("list_skills", tool_names) # list_skills is added by AdaptiveAgent
        self.assertIn("enable_skill", tool_names) # enable_skill is added by AdaptiveAgent
        self.assertIn("tool1", tool_names)
        self.assertIn("tool2", tool_names)
        self.assertEqual(agent.model, "test-model")
        self.assertEqual(agent.name, "test_agent")
        self.assertIsInstance(agent._mode_manager, ModeManager)

    @patch("dak_agent.mode_manager.ModeManager.generate_mode_config")
    async def test_initial_turn_trigger(self, mock_generate_config):
        """Test that the first turn does NOT trigger a mode switch (starts with minimal tools)."""
        agent = AdaptiveAgent(
            model="test-model",
            name="test_agent",
            instruction="Initial instruction",
            tools=self.mock_tools
        )

        # Mock generate_config
        mock_generate_config.return_value = ("New Instruction", ["tool1"], [])

        # Simulate callback (first turn)
        context = MagicMock()
        context.session.events = []
        context.state = {}
        await agent._wrapped_callback(llm_response=MagicMock(), callback_context=context)

        # Verify Switch DID NOT happen (instruction remains same)
        self.assertEqual(agent.instruction, "Initial instruction")

        # Verify generate_mode_config NOT called
        mock_generate_config.assert_not_called()

        # Verify the first turn is recorded in this session's state (it happens
        # inside ModeManager.should_switch), not on the shared ModeManager instance.
        self.assertTrue(context.state.get(FIRST_TURN_DONE_KEY))

    @patch("dak_agent.mode_manager.ModeManager.generate_mode_config")
    async def test_large_context_does_not_trigger_switch(self, mock_generate_config):
        """Context pressure is the harness's job (ADK compaction), not a mode switch."""
        agent = AdaptiveAgent(
            model="test-model",
            name="test_agent",
            instruction="Initial instruction",
            tools=self.mock_tools
        )
        event = MagicMock()
        event.content.parts = [MagicMock(text="a" * 1_000_000)]
        context = MagicMock()
        context.session.events = [event]
        context.state = {FIRST_TURN_DONE_KEY: True}

        await agent._wrapped_callback(llm_response=MagicMock(), callback_context=context)

        self.assertEqual(agent.instruction, "Initial instruction")
        mock_generate_config.assert_not_called()
        self.assertEqual(len(context.session.events), 1)

    def test_history_summary_reads_session_events(self):
        """ADK sessions keep history in `events`; the summary must read them."""
        agent = AdaptiveAgent(
            model="test-model",
            name="test_agent",
            instruction="Initial instruction",
            tools=self.mock_tools
        )
        event = MagicMock()
        event.content.parts = [MagicMock(text="please review the repo")]
        context = MagicMock()
        context.session.events = [event]

        self.assertIn("please review the repo", agent._extract_history_summary(context))

    @patch("dak_agent.mode_manager.ModeManager.generate_mode_config")
    async def test_switch_mode_tool_trigger(self, mock_generate_config):
        """Test that LLM calling switch_mode triggers a switch."""
        agent = AdaptiveAgent(
            model="test-model",
            name="test_agent",
            instruction="Initial instruction",
            tools=self.mock_tools
        )

        # Mock generate_config
        mock_generate_config.return_value = ("New Instruction", ["tool1"], [])

        # Create LLM response with switch_mode tool call
        llm_response = MagicMock()
        mock_part = MagicMock()
        mock_part.function_call = MagicMock()
        mock_part.function_call.name = "switch_mode"
        mock_part.function_call.args = {"reason": "test", "new_focus": "debugging"}
        llm_response.content.parts = [mock_part]

        context = MagicMock()
        context.session.events = []
        # Bypass initial turn trigger for this session.
        context.state = {FIRST_TURN_DONE_KEY: True}
        # google-adk v2 runs the invocation on a copy of the agent; point the
        # mock's "live copy" back at `agent` itself so the assertions below
        # can observe the switch (see AdaptiveAgent._live_agent).
        context._invocation_context.agent = agent

        await agent._wrapped_callback(llm_response=llm_response, callback_context=context)

        # Verify Switch happened
        self.assertEqual(agent.instruction, "New Instruction")
        mock_generate_config.assert_called_once()

    @patch("dak_agent.mode_manager.ModeManager.generate_mode_config")
    async def test_first_turn_is_tracked_per_session(self, mock_generate_config):
        """Regression test: one AdaptiveAgent/ModeManager is shared by every
        session, so session A's first turn must not consume session B's."""
        agent = AdaptiveAgent(
            model="test-model",
            name="test_agent",
            instruction="Initial instruction",
            tools=self.mock_tools
        )
        mock_generate_config.return_value = ("New Instruction", ["tool1"], [])

        session_a = MagicMock()
        session_a.session.events = []
        session_a.state = {}
        session_b = MagicMock()
        session_b.session.events = []
        session_b.state = {}

        # Session A's first turn.
        await agent._wrapped_callback(llm_response=MagicMock(), callback_context=session_a)
        self.assertTrue(session_a.state.get(FIRST_TURN_DONE_KEY))

        # Session B's first turn must still be untouched by session A.
        self.assertFalse(session_b.state.get(FIRST_TURN_DONE_KEY, False))
        await agent._wrapped_callback(llm_response=MagicMock(), callback_context=session_b)
        mock_generate_config.assert_not_called()
        self.assertTrue(session_b.state.get(FIRST_TURN_DONE_KEY))

    def test_call_instruction_overrides_mode_and_base_instruction(self):
        agent = AdaptiveAgent(
            model="test-model",
            name="test_agent",
            instruction="Initial instruction",
            tools=self.mock_tools
        )
        state = {"dak_mode_instruction": "Mode instruction", "dak_active_skills": []}

        self.assertEqual(agent._resolve_session_instruction(state, {}), "Mode instruction")
        self.assertEqual(
            agent._resolve_session_instruction(state, {"dak:instruction": "Answer in one word."}),
            "Answer in one word.",
        )

    def _session_context(self, agent, state):
        context = MagicMock()
        context.state = state
        context._invocation_context.agent = MagicMock()
        return context

    def test_call_model_switches_live_model_when_allowed(self):
        agent = AdaptiveAgent(model="openai/default-model", name="test_agent",
                              instruction="Initial instruction", tools=self.mock_tools)
        context = self._session_context(agent, {"dak:model": "openai/allowed-model"})

        with patch.dict(os.environ, {"DAK_ALLOWED_MODELS": "openai/allowed-model"}):
            error = agent._apply_session_config(context)

        self.assertIsNone(error)
        live = context._invocation_context.agent
        self.assertEqual(live.model.model, "openai/allowed-model")
        # The LiteLlm is cached and shared across sessions, not rebuilt per call.
        other = self._session_context(agent, {"dak:model": "openai/allowed-model"})
        with patch.dict(os.environ, {"DAK_ALLOWED_MODELS": "openai/allowed-model"}):
            agent._apply_session_config(other)
        self.assertIs(other._invocation_context.agent.model, live.model)
        self.assertEqual(agent.model, "openai/default-model")  # the shared root is untouched

    async def test_call_model_rejected_returns_error_without_calling_llm(self):
        import json

        from google.adk.apps import App
        from google.adk.artifacts import InMemoryArtifactService
        from google.adk.models.base_llm import BaseLlm
        from google.adk.runners import Runner
        from google.adk.sessions import InMemorySessionService
        from google.genai import types

        calls = []

        class MustNotBeCalled(BaseLlm):
            async def generate_content_async(self, llm_request, stream=False):
                calls.append(llm_request)
                raise AssertionError("the LLM must not be called for a refused model")
                yield  # pragma: no cover

        agent = AdaptiveAgent(model=MustNotBeCalled(model="default-model"), name="dak_agent",
                              instruction="Initial instruction", tools=[])
        sessions = InMemorySessionService()
        session = await sessions.create_session(app_name="dak_agent", user_id="u")
        runner = Runner(app=App(name="dak_agent", root_agent=agent), session_service=sessions,
                        artifact_service=InMemoryArtifactService())

        texts = []
        with patch.dict(os.environ, {"DAK_ALLOWED_MODELS": "openai/allowed-model"}), \
                patch("dak_agent.remote_tools.discover_remote_tools", return_value={}):
            async for event in runner.run_async(
                user_id="u", session_id=session.id,
                new_message=types.Content(role="user", parts=[types.Part(text="hi")]),
                state_delta={"dak:model": "openai/not-allowed"},
            ):
                texts += [p.text for p in (event.content.parts if event.content else []) if p.text]

        self.assertEqual(calls, [])
        error = json.loads(texts[-1])
        self.assertEqual(error["error"], "model_not_allowed")
        self.assertEqual(error["requested_model"], "openai/not-allowed")
        self.assertEqual(error["allowed_models"], ["openai/allowed-model"])

    async def test_call_model_rejected_even_when_session_config_fails(self):
        """A broken piece of session state must not turn the refusal into a
        silent fall-through to the default model (fail closed)."""
        import json

        agent = AdaptiveAgent(model="openai/default-model", name="test_agent",
                              instruction="Initial instruction", tools=self.mock_tools)
        context = self._session_context(agent, {"dak:model": "openai/not-allowed", "dak_active_skills": None})

        with patch.dict(os.environ, {"DAK_ALLOWED_MODELS": "openai/allowed-model"}):
            content = await agent._restore_session_config(context)

        self.assertIsNotNone(content)
        self.assertEqual(json.loads(content.parts[0].text)["error"], "model_not_allowed")

    def test_resolve_session_instruction_includes_current_plan_when_present(self):
        agent = AdaptiveAgent(model="test-model", name="test_agent",
                              instruction="Initial instruction", tools=self.mock_tools)
        state = {"dak_todos": [{"step": "read repo", "status": "done"},
                               {"step": "write summary", "status": "pending"}]}

        instruction = agent._resolve_session_instruction(state, {})

        self.assertTrue(instruction.startswith("Initial instruction"))
        self.assertIn("# Current Plan\n1. [done] read repo\n2. [pending] write summary", instruction)

    def test_resolve_session_instruction_omits_plan_section_when_absent(self):
        agent = AdaptiveAgent(model="test-model", name="test_agent",
                              instruction="Initial instruction", tools=self.mock_tools)

        self.assertNotIn("# Current Plan", agent._resolve_session_instruction({}, {}))
        self.assertNotIn("# Current Plan", agent._resolve_session_instruction({"dak_todos": []}, {}))

    async def test_plan_text_reaches_the_model_verbatim_and_base_template_still_works(self):
        """The plan is written by the model; `{name}` in it must not go through
        ADK's session-state injection (an unknown name fails the turn), while
        `{name}` in the operator's own instruction keeps working."""
        from google.adk.apps import App
        from google.adk.artifacts import InMemoryArtifactService
        from google.adk.models.base_llm import BaseLlm
        from google.adk.models.llm_response import LlmResponse
        from google.adk.runners import Runner
        from google.adk.sessions import InMemorySessionService
        from google.genai import types

        requests = []

        class RecordingLlm(BaseLlm):
            async def generate_content_async(self, llm_request, stream=False):
                requests.append(llm_request)
                yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text="ok")]))

        agent = AdaptiveAgent(model=RecordingLlm(model="recording"), name="dak_agent",
                              instruction="Hello {greeting}.", tools=[])
        sessions = InMemorySessionService()
        session = await sessions.create_session(
            app_name="dak_agent", user_id="u",
            state={"greeting": "operator", "dak_todos": [{"step": "fill {summary}", "status": "pending"}]})
        runner = Runner(app=App(name="dak_agent", root_agent=agent), session_service=sessions,
                        artifact_service=InMemoryArtifactService())

        with patch("dak_agent.remote_tools.discover_remote_tools", return_value={}):
            async for _ in runner.run_async(
                user_id="u", session_id=session.id,
                new_message=types.Content(role="user", parts=[types.Part(text="hi")]),
            ):
                pass

        system = requests[-1].config.system_instruction
        self.assertIn("Hello operator.", system)
        self.assertIn("1. [pending] fill {summary}", system)

    async def test_plan_written_mid_invocation_reaches_the_next_model_call(self):
        """Compaction happens inside long invocations, so the plan must be in
        the instruction from the model call right after write_todos, not only
        from the next turn."""
        requests = await self._plan_then_answer([{"step": "check", "status": "pending"}])

        self.assertEqual(len(requests), 2)  # one invocation, two model calls
        self.assertNotIn("# Current Plan", requests[0].config.system_instruction)
        self.assertIn("1. [pending] check", requests[1].config.system_instruction)

    async def test_long_plan_reaches_the_next_model_call_with_the_harness_plugin(self):
        """Production installs ContextHarnessPlugin, whose after_tool_callback
        replaces a long tool output; ADK then skips agent after_tool callbacks.
        The refresh must not depend on them."""
        from dak_agent.harness import ContextHarnessPlugin, HarnessSettings

        plan = [{"step": f"step {i} " + "x" * 120, "status": "pending"} for i in range(20)]
        plugin = ContextHarnessPlugin(HarnessSettings(context_window=8000), "test-model")  # 2,000-char cap
        requests = await self._plan_then_answer(plan, plugins=[plugin])

        self.assertIn("20. [pending] step 19", requests[1].config.system_instruction)

    async def _plan_then_answer(self, plan, plugins=()):
        from google.adk.apps import App
        from google.adk.artifacts import InMemoryArtifactService
        from google.adk.models.base_llm import BaseLlm
        from google.adk.models.llm_response import LlmResponse
        from google.adk.runners import Runner
        from google.adk.sessions import InMemorySessionService
        from google.adk.tools import FunctionTool
        from google.genai import types

        from dak_agent.builtin_tools import write_todos

        requests = []

        class PlanThenAnswer(BaseLlm):
            async def generate_content_async(self, llm_request, stream=False):
                requests.append(llm_request)
                if len(requests) == 1:
                    part = types.Part(function_call=types.FunctionCall(
                        id="fc-1", name="write_todos", args={"items": plan}))
                else:
                    part = types.Part(text="done")
                yield LlmResponse(content=types.Content(role="model", parts=[part]))

        agent = AdaptiveAgent(model=PlanThenAnswer(model="plan"), name="dak_agent",
                              instruction="Base.", tools=[FunctionTool(write_todos)])
        sessions = InMemorySessionService()
        session = await sessions.create_session(app_name="dak_agent", user_id="u")
        runner = Runner(app=App(name="dak_agent", root_agent=agent, plugins=list(plugins)),
                        session_service=sessions, artifact_service=InMemoryArtifactService())

        with patch("dak_agent.remote_tools.discover_remote_tools", return_value={}):
            async for _ in runner.run_async(
                user_id="u", session_id=session.id,
                new_message=types.Content(role="user", parts=[types.Part(text="hi")]),
            ):
                pass
        return requests

    def test_call_instruction_replaces_the_plan_too(self):
        """`dak:instruction` makes the system prompt exactly the caller's text
        (PBI #137), so the session plan is not appended to it."""
        agent = AdaptiveAgent(model="test-model", name="test_agent",
                              instruction="Initial instruction", tools=self.mock_tools)
        state = {"dak_todos": [{"step": "read repo", "status": "done"}]}

        self.assertEqual(agent._resolve_session_instruction(state, {"dak:instruction": "Only this."}), "Only this.")

    def test_plan_section_is_capped_by_the_window(self):
        agent = AdaptiveAgent(model="test-model", name="test_agent",
                              instruction="Initial instruction", tools=self.mock_tools)
        agent._mode_manager.max_context_tokens = 8192  # plan_chars == 1,000
        state = {"dak_todos": [{"step": f"step {i} " + "z" * 30, "status": "pending"} for i in range(200)]}

        instruction = agent._resolve_session_instruction(state, {})

        plan = instruction.split("# Current Plan\n", 1)[1]
        self.assertLessEqual(len(plan), 1_000)
        self.assertIn("Call read_plan for the whole plan.", plan)

        agent._mode_manager.max_context_tokens = 32_768  # plan_chars == 1,638
        larger = agent._resolve_session_instruction(state, {}).split("# Current Plan\n", 1)[1]
        self.assertGreater(len(larger), 1_000)
        self.assertLessEqual(len(larger), 1_638)

    def _tools_agent(self):
        from dak_agent.builtin_tools import switch_mode

        default_mcp = MagicMock()
        type(default_mcp).__name__ = "McpToolset"
        return AdaptiveAgent(model="test-model", name="test_agent", instruction="x",
                             tools=[FunctionTool(switch_mode), default_mcp])

    def test_call_tools_empty_list_removes_all_tools(self):
        agent = self._tools_agent()
        self.assertEqual(agent._resolve_session_tools({}, {"dak:tools": []}), [])

    def test_call_tools_subset_selects_named_tools_only(self):
        agent = self._tools_agent()
        tools = agent._resolve_session_tools({"dak_active_skills": ["anything"]}, {"dak:tools": ["switch_mode"]})
        self.assertEqual([t.name for t in tools], ["switch_mode"])

    def test_call_tools_names_not_built_in_come_from_the_default_mcp_server(self):
        agent = self._tools_agent()
        agent.available_remote_tools = {"read_file": "", "grep": "", "run_command": ""}
        with patch("dak_agent.skill_tools.make_mcp_toolset", return_value=MagicMock(name="toolset")) as make:
            tools = agent._resolve_session_tools({}, {"dak:tools": ["switch_mode", "read_file", "grep"]})
        self.assertEqual(len(tools), 2)
        make.assert_called_once_with(agent.mcp_url, "http", ["grep", "read_file"])

    def test_call_tools_unknown_names_never_build_mcp_toolsets(self):
        """The toolset cache is keyed by names; names the default MCP server
        does not have must not create (and keep) a new connection each call."""
        agent = self._tools_agent()
        agent.available_remote_tools = {"read_file": ""}
        with patch("dak_agent.skill_tools.make_mcp_toolset", return_value=MagicMock(name="toolset")) as make:
            for i in range(5):
                agent._resolve_session_tools({}, {"dak:tools": [f"x{i}"]})
            agent._resolve_session_tools({}, {"dak:tools": ["read_file", "nope"]})
        make.assert_called_once_with(agent.mcp_url, "http", ["read_file"])
        self.assertEqual(len(agent._mcp_toolset_cache), 1)

    def test_call_tools_without_transfer_to_agent_drops_sub_agents_for_the_call(self):
        from google.adk.agents import LlmAgent

        agent = AdaptiveAgent(model="test-model", name="test_agent", instruction="x", tools=[],
                              sub_agents=[LlmAgent(name="peer", model="test-model")])
        for call_tools, expected in (([], []), (["transfer_to_agent"], ["peer"]),
                                     ({"names": ["transfer_to_agent"]}, ["peer"]),
                                     ({"mcp_servers": []}, [])):
            live = agent.model_copy()
            context = MagicMock()
            context.state = {}
            context._invocation_context.agent = live
            with patch("dak_agent.call_config.resolve_dak_settings", return_value={"dak:tools": call_tools}):
                agent._apply_session_config(context)
            self.assertEqual([a.name for a in live.sub_agents], expected)
        self.assertEqual([a.name for a in agent.sub_agents], ["peer"])  # the shared root is untouched

    async def test_call_tools_fail_closed_when_session_config_fails(self):
        agent = self._tools_agent()
        live = agent.model_copy()
        context = MagicMock()
        context.state = {}
        context._invocation_context.agent = live
        with patch("dak_agent.call_config.resolve_dak_settings", return_value={"dak:tools": []}), \
                patch.object(AdaptiveAgent, "_resolve_session_instruction", side_effect=RuntimeError("boom")):
            await agent._restore_session_config(context)
        self.assertEqual(live.tools, [])

    def test_without_call_tools_the_session_config_is_used_as_before(self):
        agent = self._tools_agent()
        names = [getattr(t, "name", None) for t in agent._resolve_session_tools({}, {})]
        self.assertIn("switch_mode", names)
        self.assertIn("enable_skill", names)

class TestRestoreRejectReason(unittest.IsolatedAsyncioTestCase):
    """ADK answers a rejected confirmation with a fixed text; the reason the
    user gave travels in the confirmation payload (docs/design/approval-queue.md)."""

    REJECTED = {"error": "This tool call is rejected."}

    def _agent(self):
        return AdaptiveAgent(model="test-model", name="test_agent", instruction="i", tools=[])

    def _context(self, confirmed, payload):
        from google.adk.tools.tool_confirmation import ToolConfirmation
        ctx = MagicMock()
        ctx.tool_confirmation = ToolConfirmation(confirmed=confirmed, payload=payload)
        return ctx

    def test_restore_reject_reason_rewrites_rejected_observation(self):
        ctx = self._context(False, {"mode": "reject", "reason": "not now"})
        result = self._agent()._restore_reject_reason(MagicMock(), {}, ctx, self.REJECTED)
        self.assertEqual(result, {"observation": "denied_by_user", "reason": "not now"})

    def test_restore_reject_reason_reports_timeout(self):
        ctx = self._context(False, {"mode": "timed_out", "reason": ""})
        result = self._agent()._restore_reject_reason(MagicMock(), {}, ctx, self.REJECTED)
        self.assertEqual(result, {"observation": "timed_out"})

    def test_restore_reject_reason_is_noop_without_payload(self):
        """The CLI answers with `confirmed` only: the fixed text stays."""
        ctx = self._context(False, None)
        self.assertIsNone(self._agent()._restore_reject_reason(MagicMock(), {}, ctx, self.REJECTED))

    def test_restore_reject_reason_is_noop_for_other_results(self):
        """Only ADK's own rejection text is rewritten, even after a rejection."""
        ctx = self._context(False, {"mode": "reject", "reason": "not now"})
        self.assertIsNone(self._agent()._restore_reject_reason(MagicMock(), {}, ctx, {"result": "ok"}))

    async def test_rejected_confirmation_reaches_the_model_with_its_reason(self):
        """Through ADK's Runner: a tool asks for confirmation, the pending
        approval is listed, a reject with a reason resumes the session, and
        the next model call sees the reason instead of ADK's fixed text."""
        from google.adk.apps import App
        from google.adk.models.base_llm import BaseLlm
        from google.adk.models.llm_response import LlmResponse
        from google.adk.runners import Runner
        from google.adk.sessions import InMemorySessionService
        from google.adk.tools import FunctionTool
        from google.genai import types

        from dak_agent import approvals

        def touch(path: str) -> str:
            """Create a file."""
            return f"touched {path}"

        requests = []

        class CallThenAnswer(BaseLlm):
            async def generate_content_async(self, llm_request, stream=False):
                requests.append(llm_request)
                if len(requests) == 1:
                    part = types.Part(function_call=types.FunctionCall(id="fc-1", name="touch", args={"path": "a"}))
                else:
                    part = types.Part(text="ok")
                yield LlmResponse(content=types.Content(role="model", parts=[part]))

        agent = AdaptiveAgent(model=CallThenAnswer(model="m"), name="dak_agent", instruction="i",
                              tools=[FunctionTool(touch, require_confirmation=True)])
        sessions = InMemorySessionService()
        session = await sessions.create_session(app_name="dak_agent", user_id="u")
        runner = Runner(app=App(name="dak_agent", root_agent=agent), session_service=sessions)

        async def run(message):
            with patch("dak_agent.remote_tools.discover_remote_tools", return_value={}):
                async for _ in runner.run_async(user_id="u", session_id=session.id,
                                                new_message=types.Content.model_validate(message)):
                    pass
            stored = await sessions.get_session(app_name="dak_agent", user_id="u", session_id=session.id)
            return [e.model_dump(mode="json", by_alias=True, exclude_none=True) for e in stored.events]

        [pending] = approvals.list_pending(await run({"role": "user", "parts": [{"text": "hi"}]}))
        self.assertEqual((pending["kind"], pending["tool_name"]), ("approval", "touch"))

        reply = approvals.build_reply_function_response(pending["id"], "reject", "not now")
        self.assertEqual(approvals.list_pending(await run({"role": "user", **reply})), [])

        responses = [p.function_response.response for c in requests[1].contents for p in c.parts
                     if p.function_response and p.function_response.name == "touch"]
        self.assertEqual(responses[-1], {"observation": "denied_by_user", "reason": "not now"})


if __name__ == '__main__':
    unittest.main()
