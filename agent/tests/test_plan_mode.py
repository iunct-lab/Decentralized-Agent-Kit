"""Tests for Plan mode's permission rules (dak_agent.plan_mode)."""
import asyncio

import pytest

from dak_agent import plan_mode
from dak_agent.permission import DEFAULT_RULES, PERSISTED_ALLOW_KEY, always_approvals, evaluate, strictest
from test_permission import Harness, _state_event, mcp_tool, responses


def judge(state, tool, args, source="default"):
    """What the permission plugin decides: the configured rules, tightened by Plan mode."""
    action = evaluate(DEFAULT_RULES, source, tool, args, always_approvals(state))
    return strictest(action, plan_mode.check(state, source, tool, args))


def active():
    state = {}
    plan_mode.enter(state)
    return state


@pytest.mark.parametrize("path", ["a.txt", "agent/dak_agent/x.py", "plans/x.txt", "/plans/x.md", "plans/../agent/x.md"])
def test_write_file_denied_outside_plan_glob(path):
    assert judge(active(), "write_file", {"path": path}) == "deny"


def test_write_file_allowed_for_plan_glob():
    # Plan mode does not loosen anything: the plan file asks as it does outside Plan mode.
    assert judge(active(), "write_file", {"path": "plans/x.md"}) == judge({}, "write_file", {"path": "plans/x.md"})
    assert judge(active(), "write_file", {"path": "plans/x.md"}, source="local") == "allow"


def test_edit_file_denied_outside_plan_glob():
    assert judge(active(), "edit_file", {"path": "a.py"}) == "deny"
    assert judge(active(), "edit_file", {"path": "plans/x.md"}) != "deny"


@pytest.mark.parametrize("command", ["git status", "rm x", "ls"])
def test_run_command_denied_in_plan_mode(command):
    assert judge(active(), "run_command", {"command": command}) == "deny"


def test_reads_are_unchanged_in_plan_mode():
    for tool, args in (("read_file", {"path": "a.py"}), ("grep", {"path": "."}), ("planner", {})):
        assert judge(active(), tool, args) == judge({}, tool, args)


def test_rules_empty_when_plan_mode_inactive():
    state = {}
    assert plan_mode.active_rules(state) == []
    plan_mode.enter(state)
    assert plan_mode.is_active(state)
    plan_mode.exit_(state)
    assert plan_mode.active_rules(state) == []


def test_always_approval_does_not_override_plan_mode():
    state = active()
    state[PERSISTED_ALLOW_KEY] = [{"source": "default", "tool": "write_file", "pattern": "a.txt"}]
    assert judge(state, "write_file", {"path": "a.txt"}) == "deny"


def test_plugin_returns_denied_observation_in_plan_mode():
    tool = mcp_tool("write_file")
    h = Harness(tool, {"name": "write_file", "args": {"path": "a.txt"}})
    asyncio.run(h.sessions.append_event(h.session, _state_event({plan_mode.PLAN_MODE_KEY: True})))
    h.session = asyncio.run(h.sessions.get_session(app_name="dak_agent", user_id="u", session_id=h.session.id))

    events = h.say("write")

    assert tool.calls == []
    denied = responses(events, "write_file")[0]
    assert denied["observation"] == "denied_by_policy"
    assert "Plan mode" in denied["reason"]
