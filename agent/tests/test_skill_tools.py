import os
import shutil

import pytest
import tempfile
import unittest
from unittest.mock import patch

from dak_agent import mcp_headers
from dak_agent.skill_tools import (
    WALLET_TOOL_NAMES,
    load_local_tools_from_skill,
    load_solana_wallet_tools,
    make_mcp_toolset,
)


class TestLoadLocalToolsFromSkill(unittest.TestCase):
    def setUp(self):
        self.skill_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.skill_dir)

    def _write_tools_py(self, content: str):
        with open(os.path.join(self.skill_dir, "tools.py"), "w") as f:
            f.write(content)

    def test_no_tools_py_falls_back_to_mcp(self):
        tools, mcp_fallback = load_local_tools_from_skill(
            "myskill", self.skill_dir, ["tool_a", "tool_b"], current_tool_names=[]
        )
        self.assertEqual(tools, [])
        self.assertEqual(mcp_fallback, ["tool_a", "tool_b"])

    def test_loads_callable_tools(self):
        self._write_tools_py(
            "def tool_a(x: str) -> str:\n"
            "    \"\"\"Tool A.\"\"\"\n"
            "    return x\n"
        )
        tools, mcp_fallback = load_local_tools_from_skill(
            "myskill_loads", self.skill_dir, ["tool_a", "tool_b"], current_tool_names=[]
        )
        self.assertEqual([t.name for t in tools], ["tool_a"])
        self.assertEqual(mcp_fallback, ["tool_b"])

    def test_skips_already_present_tools(self):
        self._write_tools_py(
            "def tool_a(x: str) -> str:\n"
            "    \"\"\"Tool A.\"\"\"\n"
            "    return x\n"
        )
        tools, mcp_fallback = load_local_tools_from_skill(
            "myskill_present", self.skill_dir, ["tool_a"], current_tool_names=["tool_a"]
        )
        self.assertEqual(tools, [])
        self.assertEqual(mcp_fallback, [])

    def test_non_callable_falls_back(self):
        self._write_tools_py("tool_a = 42\n")
        tools, mcp_fallback = load_local_tools_from_skill(
            "myskill_noncallable", self.skill_dir, ["tool_a"], current_tool_names=[]
        )
        self.assertEqual(tools, [])
        self.assertEqual(mcp_fallback, ["tool_a"])

    def test_broken_module_falls_back(self):
        self._write_tools_py("raise RuntimeError('boom')\n")
        tools, mcp_fallback = load_local_tools_from_skill(
            "myskill_broken", self.skill_dir, ["tool_a"], current_tool_names=[]
        )
        self.assertEqual(tools, [])
        self.assertEqual(mcp_fallback, ["tool_a"])


class TestMakeMcpToolset(unittest.TestCase):
    @patch("dak_agent.skill_tools.McpToolset")
    def test_http_toolset(self, MockToolset):
        make_mcp_toolset("http://srv:8000/mcp", "http", ["tool_a"])
        kwargs = MockToolset.call_args.kwargs
        self.assertEqual(kwargs["tool_filter"], ["tool_a"])
        self.assertEqual(kwargs["connection_params"].url, "http://srv:8000/mcp")

    @patch("dak_agent.skill_tools.McpToolset")
    def test_sse_toolset(self, MockToolset):
        make_mcp_toolset("http://srv:8000/sse", "sse", None)
        kwargs = MockToolset.call_args.kwargs
        self.assertEqual(kwargs["connection_params"].url, "http://srv:8000/sse")
        self.assertEqual(type(kwargs["connection_params"]).__name__, "SseConnectionParams")

    def test_make_mcp_toolset_wires_session_key_header(self):
        toolset = make_mcp_toolset("http://srv:8000/mcp", "http", ["tool_a"])
        self.assertIs(toolset.header_provider, mcp_headers.session_key_header)


class TestLoadSolanaWalletTools(unittest.TestCase):
    def test_loads_wallet_tools(self):
        tools = load_solana_wallet_tools()
        self.assertEqual([t.name for t in tools], WALLET_TOOL_NAMES)

    def test_skips_existing(self):
        tools = load_solana_wallet_tools(existing_tool_names=["check_solana_balance"])
        names = [t.name for t in tools]
        self.assertNotIn("check_solana_balance", names)
        self.assertIn("send_sol_payment", names)


if __name__ == "__main__":
    unittest.main()


def _make_agent_with_demo_skill():
    """An AdaptiveAgent whose registry has one instructions-only skill "demo",
    for tests that don't care about local-tool/MCP loading specifics."""
    from unittest.mock import MagicMock

    from dak_agent.adaptive_agent import AdaptiveAgent
    from dak_agent.skill_registry import SkillRegistry

    agent = AdaptiveAgent(model="test-model", name="root", instruction="base", tools=[])
    agent.skill_registry = MagicMock(spec=SkillRegistry)
    agent.skill_registry.find_skill_dir.return_value = "/tmp/nonexistent-demo-skill-dir"
    agent.skill_registry.get_skill.side_effect = lambda name: {
        "name": "demo", "instructions": "Use the demo skill.", "tools": [],
    } if name == "demo" else None
    return agent


class TestApplySessionConfig(unittest.TestCase):
    """`_apply_session_config` rebuilds a session's instruction/tools from its
    `state` and applies them to the live per-invocation agent copy, never to
    the shared root instance (agent/dak_agent/adaptive_agent.py). Regression
    coverage for the PBI #133 bug: one AdaptiveAgent instance is shared by
    every session in the process."""

    def test_root_agent_is_never_mutated(self):
        from unittest.mock import MagicMock

        agent = _make_agent_with_demo_skill()
        live = agent.model_copy()
        ctx = MagicMock()
        ctx._invocation_context.agent = live
        ctx.state = {"dak_active_skills": ["demo"]}

        agent._apply_session_config(ctx)

        self.assertIn("Use the demo skill.", live.instruction)
        self.assertEqual(agent.instruction, "base")
        self.assertIsNot(live.tools, agent.tools)

    def test_two_sessions_do_not_share_enabled_skills(self):
        """Regression test: session A enabling a skill must not change what
        session B's live copy resolves to, even though both invocations share
        the same root AdaptiveAgent instance."""
        from unittest.mock import MagicMock

        agent = _make_agent_with_demo_skill()
        live_a = agent.model_copy()
        live_b = agent.model_copy()
        ctx_a = MagicMock()
        ctx_a._invocation_context.agent = live_a
        ctx_a.state = {"dak_active_skills": ["demo"]}
        ctx_b = MagicMock()
        ctx_b._invocation_context.agent = live_b
        ctx_b.state = {}

        agent._apply_session_config(ctx_a)
        agent._apply_session_config(ctx_b)

        self.assertIn("Use the demo skill.", live_a.instruction)
        self.assertNotIn("Use the demo skill.", live_b.instruction)

    def test_restored_after_new_instance_for_same_session(self):
        """Regression test: a brand-new AdaptiveAgent instance (e.g. after a
        process restart) continuing the same session's `state` must restore
        the skill enabled by a previous instance."""
        from unittest.mock import MagicMock

        state = {"dak_active_skills": ["demo"]}

        old_instance = _make_agent_with_demo_skill()
        old_live = old_instance.model_copy()
        ctx = MagicMock()
        ctx._invocation_context.agent = old_live
        ctx.state = state
        old_instance._apply_session_config(ctx)
        self.assertIn("Use the demo skill.", old_live.instruction)

        # A fresh instance, as if the process were redeployed/restarted.
        new_instance = _make_agent_with_demo_skill()
        new_live = new_instance.model_copy()
        ctx2 = MagicMock()
        ctx2._invocation_context.agent = new_live
        ctx2.state = state  # same session's persisted state

        new_instance._apply_session_config(ctx2)

        self.assertIn("Use the demo skill.", new_live.instruction)
        self.assertEqual(new_live.active_skills, ["demo"])


class TestResolveSessionTools(unittest.TestCase):
    """Paths inside `AdaptiveAgent._resolve_session_tools` that rebuild the
    tool list from session state."""

    def _agent(self, skills):
        from unittest.mock import MagicMock

        from dak_agent.adaptive_agent import AdaptiveAgent
        from dak_agent.skill_registry import SkillRegistry

        agent = AdaptiveAgent(model="test-model", name="root", instruction="base", tools=[],
                              mcp_url="http://default")
        agent.skill_registry = MagicMock(spec=SkillRegistry)
        agent.skill_registry.find_skill_dir.side_effect = (
            lambda name: f"/tmp/nonexistent-{name}" if name in skills else None)
        agent.skill_registry.get_skill.side_effect = skills.get
        return agent

    def _toolsets(self, tools):
        return {(t.url, t.conn_type): t.tool_filter for t in tools if getattr(t, "is_fake_toolset", False)}

    def _fake_make_mcp_toolset(self):
        from unittest.mock import MagicMock

        def make(url, conn_type="http", tool_filter=None):
            ts = MagicMock()
            ts.is_fake_toolset = True
            ts.url, ts.conn_type, ts.tool_filter = url, conn_type, tool_filter
            ts.name = None
            return ts
        return make

    def test_skill_mcp_server_routes_to_its_own_server(self):
        agent = self._agent({
            "extra_skill": {"name": "extra_skill", "tools": ["t1"], "mcp_server": "extra"},
            "plain": {"name": "plain", "tools": ["t2"]},
        })
        agent._mcp_servers = {"extra": {"name": "extra", "url": "http://extra", "type": "sse"}}
        with patch("dak_agent.skill_tools.make_mcp_toolset", side_effect=self._fake_make_mcp_toolset()):
            tools = agent._resolve_session_tools({"dak_active_skills": ["extra_skill", "plain"]}, {})
        self.assertEqual(self._toolsets(tools), {
            ("http://extra", "sse"): ["t1"],
            ("http://default", "http"): ["t2"],
        })

    def test_mode_switch_selecting_nothing_falls_back_to_unfiltered_default(self):
        agent = self._agent({})
        agent._has_default_mcp_toolset = True
        with patch("dak_agent.skill_tools.make_mcp_toolset", side_effect=self._fake_make_mcp_toolset()):
            tools = agent._resolve_session_tools({"dak_mode_tool_names": []}, {})
        self.assertEqual(self._toolsets(tools), {("http://default", "http"): None})

    def test_no_mode_switch_means_no_mcp_toolset(self):
        agent = self._agent({})
        agent._has_default_mcp_toolset = True
        with patch("dak_agent.skill_tools.make_mcp_toolset", side_effect=self._fake_make_mcp_toolset()):
            tools = agent._resolve_session_tools({}, {})
        self.assertEqual(self._toolsets(tools), {})

    def test_ap2_attaches_wallet_tools_alongside_a_paid_skill(self):
        agent = self._agent({"paid": {"name": "paid", "tools": []}})
        agent._enable_ap2 = True
        names = {getattr(t, "name", None) for t in agent._resolve_session_tools({"dak_active_skills": ["paid"]}, {})}
        self.assertTrue(set(WALLET_TOOL_NAMES) <= names)

    def test_mcp_toolsets_are_reused_across_turns_and_sessions(self):
        """A fresh McpToolset per turn would leak one MCP connection per turn."""
        agent = self._agent({"plain": {"name": "plain", "tools": ["t2"]}})
        with patch("dak_agent.skill_tools.make_mcp_toolset", side_effect=self._fake_make_mcp_toolset()) as make:
            first = agent._resolve_session_tools({"dak_active_skills": ["plain"]}, {})
            second = agent._resolve_session_tools({"dak_active_skills": ["plain"]}, {})
        self.assertEqual(make.call_count, 1)
        self.assertIs(first[-1], second[-1])


class TestModeSwitchResetsSkills(unittest.IsolatedAsyncioTestCase):
    async def test_switch_replaces_active_skills_with_selection(self):
        from unittest.mock import MagicMock

        agent = _make_agent_with_demo_skill()
        ctx = MagicMock()
        ctx._invocation_context.agent = agent.model_copy()
        ctx.session.events = []
        ctx.state = {"dak_active_skills": ["demo"]}
        with patch("dak_agent.mode_manager.ModeManager.generate_mode_config",
                   return_value=("Focused.", [], [])):
            await agent._perform_mode_switch(ctx)
        self.assertEqual(ctx.state["dak_active_skills"], [])
        self.assertNotIn("Use the demo skill.", ctx._invocation_context.agent.instruction)


class TestEnableSkillMissingDirectory(unittest.IsolatedAsyncioTestCase):
    async def test_reports_error_and_does_not_record_skill(self):
        from unittest.mock import AsyncMock, MagicMock

        agent = _make_agent_with_demo_skill()
        agent.skill_registry.find_skill_dir.return_value = None
        enable = next(t for t in agent.tools if t.name == "enable_skill").func
        ctx = MagicMock()
        ctx.state = {}
        ctx._invocation_context.agent = agent
        with patch("dak_agent.remote_tools.discover_remote_tools", AsyncMock(return_value={})):
            result = await enable(skill_name="demo", tool_context=ctx)
        self.assertIn("not found", result)
        self.assertNotIn("dak_active_skills", ctx.state)


@pytest.mark.asyncio
async def test_enable_skill_says_so_when_tools_are_fixed_by_the_call():
    """With `dak:tools`, the call's tools are fixed; enable_skill must not claim
    success (the skill's tools would never appear)."""
    from unittest.mock import MagicMock, patch as _patch

    from dak_agent import skill_tools

    agent = MagicMock()
    tool_context = MagicMock()
    tool_context.state = {}
    enable_skill = next(t for t in skill_tools.make_skill_tools(agent) if t.name == "enable_skill").func
    with _patch("dak_agent.call_config.resolve_dak_settings", return_value={"dak:tools": ["enable_skill"]}):
        result = await enable_skill("filesystem", tool_context=tool_context)
    assert "dak:tools" in result
    assert "dak_active_skills" not in tool_context.state
