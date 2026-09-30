"""Tests for the declarative tool permission rules (dak_agent.permission)."""
import asyncio
from unittest.mock import patch

import pytest
from google.adk.agents import LlmAgent
from google.adk.apps import App
from google.adk.artifacts import InMemoryArtifactService
from google.adk.events import Event, EventActions
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools import FunctionTool
from google.adk.tools.mcp_tool import McpToolset, StreamableHTTPConnectionParams
from google.adk.tools.mcp_tool.mcp_session_manager import MCPSessionManager
from google.adk.tools.mcp_tool.mcp_tool import McpTool
from google.genai import types
from mcp.types import Tool as McpBaseTool

from dak_agent import enforcer
from dak_agent.adaptive_agent import AdaptiveAgent
from dak_agent.config import load_agent_config
from dak_agent.permission import (
    DEFAULT_RULES,
    PERSISTED_ALLOW_KEY,
    PermissionPlugin,
    Rule,
    always_approvals,
    evaluate,
    evaluate_ruleset,
    load_rules,
    record_always_approval,
    split_command_segments,
    strictest,
    tool_source,
)
from dak_agent.skill_tools import STATE_ACTIVE_SKILLS, STATE_MODE_INSTRUCTION, STATE_MODE_TOOL_NAMES, make_mcp_toolset


def run(command, rules=DEFAULT_RULES, source="default"):
    return evaluate(rules, source, "run_command", {"command": command})


def test_same_ruleset_last_rule_wins():
    allow = Rule("default", "write_file", "*", "allow")
    deny = Rule("default", "write_file", "*", "deny")
    assert evaluate_ruleset([allow, deny], "default", "write_file", "x") == "deny"
    assert evaluate_ruleset([deny, allow], "default", "write_file", "x") == "allow"
    assert evaluate_ruleset([], "default", "write_file", "x") == "ask"


def test_compound_command_strictest_wins():
    assert run("git status") == "allow"
    assert run("git status && rm -rf /tmp/x") == "deny"
    assert run("git status && git log") == "allow"
    assert run("git status && ls") == "ask"
    assert run("git status;rm -rf /tmp/x") == "deny"
    assert run("git log | head") == "ask"
    # Same rule list, one segment each: last-wins decides, strictest combines.
    assert strictest("allow", "ask", "allow") == "ask"
    assert strictest("allow", "deny", "ask") == "deny"


def test_split_command_segments():
    assert split_command_segments("git status && git log -n 1") == ["git status", "git log -n 1"]
    assert split_command_segments("a||b;c|d&e") == ["a", "b", "c", "d", "e"]


@pytest.mark.parametrize("command", [
    "git log $(rm -rf /)",
    "git log `rm -rf /`",
    "git status\nrm x",
    "git status > out.txt",
    "git diff 2>&1",
    "(git status)",
    "git log 'unterminated",
])
def test_unsplittable_command_is_never_allowed(command):
    assert split_command_segments(command) is None
    assert run(command) == "ask"


@pytest.mark.parametrize("command", ["git status a#; rm -rf /", "git log --grep=x#y && rm -rf ~"])
def test_hash_inside_a_word_does_not_hide_a_chained_command(command):
    """The shell starts a comment only at the start of a word."""
    assert run(command) == "deny"


@pytest.mark.parametrize("command", [
    "git diff --output=/projects/x.yml", "git log -p --output=x", "git show --ext-diff HEAD", "git diff --textconv",
    "git diff --output=$HOME/.bashrc", "git diff --output='/tmp/a b'", "git diff --output=~/x", "git diff --ext-diff=$X",
])
def test_read_only_git_that_writes_or_runs_programs_asks(command):
    assert run(command) == "ask"


@pytest.mark.parametrize("command", ["rm -r -f /", "/bin/rm -rf /", "sudo rm -rf /"])
def test_other_spellings_of_destructive_commands_are_at_least_ask(command):
    """Other spellings than the deny patterns: never allowed."""
    assert run(command) in ("ask", "deny")


@pytest.mark.parametrize("command", [
    "git diff --outpu{t,t}=FILE", "git show --ext-dif{f,f} HEAD", "git log *.py", "git diff ~/x", "git log $REF",
    "git log --since=~x",
])
def test_segments_the_shell_expands_are_never_allowed(command):
    assert run(command) == "ask"


def test_expansion_keeps_deny_and_plain_git_allowed():
    assert run("git status && rm -rf *") == "deny"
    assert run("git log HEAD~1") == "allow"
    assert run("git log --oneline -n 3") == "allow"
    assert run("git log [ab]*") == "ask"


def test_expansion_rules_are_only_defaults():
    """Only the default server's git allows; the caller's tools and an
    operator's own allow rule are not overridden."""
    assert run("ls *.py", source="caller") == "allow"
    assert run("echo $HOME", source="caller") == "allow"
    assert run("ls *.py", DEFAULT_RULES + [Rule("default", "run_command", "ls *", "allow")]) == "allow"


def test_unsplittable_command_still_honours_deny():
    assert run("rm -rf / > /dev/null") == "deny"


@pytest.mark.parametrize("command", [
    "rm -rf /", "rm -fr build", "git push --force", "git push -f origin main", "git push origin main --force",
])
def test_default_denies_destructive_commands(command):
    assert run(command) == "deny"


def test_rules_are_keyed_by_source():
    args = {"path": "README.md"}
    assert evaluate(DEFAULT_RULES, "default", "read_file", args) == "allow"
    assert evaluate(DEFAULT_RULES, "http://other:9000/mcp", "read_file", args) == "ask"
    assert evaluate(DEFAULT_RULES, "caller", "write_file", args) == "allow"
    assert evaluate(DEFAULT_RULES, "local", "planner", {}) == "allow"
    assert evaluate(DEFAULT_RULES, "default", "write_file", args) == "ask"
    for path in (".env", "config/.env.local", ".env.production"):
        assert evaluate(DEFAULT_RULES, "default", "read_file", {"path": path}) == "ask"
        assert evaluate(DEFAULT_RULES, "default", "grep", {"pattern": ".", "path": path}) == "ask"


# --- PermissionPlugin through ADK's Runner (#177) ---

DEFAULT_URL = "http://mcp-server:8000/mcp"


def mcp_tool(name, url=DEFAULT_URL):
    """A real McpTool as make_mcp_toolset builds it (require_confirmation=False),
    whose MCP call is replaced by a recorder."""
    tool = McpTool(
        mcp_tool=McpBaseTool(name=name, inputSchema={"type": "object", "properties": {}}),
        mcp_session_manager=MCPSessionManager(StreamableHTTPConnectionParams(url=url)),
        require_confirmation=False,
    )
    tool.calls = []

    async def run(*, args, tool_context, credential=None):
        tool.calls.append(args)
        return {"ok": True}

    object.__setattr__(tool, "_run_async_impl", run)
    return tool


class ScriptedLlm(BaseLlm):
    """Calls `call` once, then answers with text. Records what it was sent."""
    call: dict
    requests: list = []

    async def generate_content_async(self, llm_request, stream=False):
        self.requests.append(llm_request)
        if len(self.requests) == 1:
            part = types.Part(function_call=types.FunctionCall(id="fc-1", **self.call))
        else:
            part = types.Part(text="done")
        yield LlmResponse(content=types.Content(role="model", parts=[part]))


class Harness:
    def __init__(self, tool, call, rules=DEFAULT_RULES, agent=None):
        if agent is None:
            self.llm = ScriptedLlm(model="scripted", call=call, requests=[])
            agent = LlmAgent(model=self.llm, name="dak_agent", instruction="x", tools=[tool])
        else:
            self.llm = agent.model
        self.sessions = InMemorySessionService()
        self.runner = Runner(
            app=App(name="dak_agent", root_agent=agent, plugins=[PermissionPlugin(rules, DEFAULT_URL)]),
            session_service=self.sessions, artifact_service=InMemoryArtifactService(),
        )
        self.session = asyncio.run(self.sessions.create_session(app_name="dak_agent", user_id="u"))

    def send(self, message):
        async def go():
            return [e async for e in self.runner.run_async(user_id="u", session_id=self.session.id, new_message=message)]
        return asyncio.run(go())

    def say(self, text):
        return self.send(types.Content(role="user", parts=[types.Part(text=text)]))

    def answer(self, events, confirmed, payload=None):
        request = next(fc for e in events for fc in e.get_function_calls() if fc.name == "adk_request_confirmation")
        response = {"confirmed": confirmed} | ({"payload": payload} if payload else {})
        return self.send(types.Content(role="user", parts=[types.Part(function_response=types.FunctionResponse(
            id=request.id, name="adk_request_confirmation", response=response))]))


def _state_event(delta):
    return Event(author="user", invocation_id="setup", actions=EventActions(state_delta=delta))


def responses(events, name):
    return [fr.response for e in events for fr in e.get_function_responses() if fr.name == name]


def test_allowed_git_status_runs_without_confirmation():
    tool = mcp_tool("run_command")
    h = Harness(tool, {"name": "run_command", "args": {"command": "git status"}})

    events = h.say("status?")

    assert tool.calls == [{"command": "git status"}]
    assert not responses(events, "adk_request_confirmation")
    assert not [fc for e in events for fc in e.get_function_calls() if fc.name == "adk_request_confirmation"]


def test_ask_holds_the_call_for_confirmation():
    for confirmed, expected_calls in ((True, [{"command": "rm x"}]), (False, [])):
        tool = mcp_tool("run_command")
        h = Harness(tool, {"name": "run_command", "args": {"command": "rm x"}})

        events = h.say("remove x")
        assert tool.calls == []  # held, not run
        assert [fc for e in events for fc in e.get_function_calls() if fc.name == "adk_request_confirmation"]

        answered = h.answer(events, confirmed)
        assert tool.calls == expected_calls
        if not confirmed:
            assert responses(answered, "run_command") == [{"error": "This tool call is rejected."}]


def test_denied_command_returns_observation_without_executing():
    tool = mcp_tool("run_command")
    h = Harness(tool, {"name": "run_command", "args": {"command": "rm -rf /"}})

    events = h.say("wipe")

    assert tool.calls == []
    denied = responses(events, "run_command")[0]
    assert denied["observation"] == "denied_by_policy"
    assert denied["rules"] == ["default run_command 'rm -rf *' -> deny"]
    # The Observation reaches the model, which answers on its own.
    sent = [p.function_response.response for c in h.llm.requests[1].contents for p in c.parts if p.function_response]
    assert sent[0]["observation"] == "denied_by_policy"


@pytest.mark.parametrize("session_state", [
    {STATE_MODE_TOOL_NAMES: ["write_file"]},  # after a mode switch
    {STATE_ACTIVE_SKILLS: ["filesystem"]},  # after enable_skill (no local tools.py: MCP fallback)
])
def test_rules_apply_to_toolsets_built_without_confirmation(session_state):
    """AdaptiveAgent rebuilds the session's MCP tools with make_mcp_toolset
    (require_confirmation=False); the plugin still asks."""
    tool = mcp_tool("write_file")
    built = []

    async def get_tools(toolset, readonly_context=None):
        built.append(toolset)
        return [tool]

    llm = ScriptedLlm(model="scripted", call={"name": "write_file", "args": {"path": "a.txt"}}, requests=[])
    agent = AdaptiveAgent(model=llm, name="dak_agent", instruction="x",
                          tools=[make_mcp_toolset(DEFAULT_URL)], mcp_url=DEFAULT_URL)
    h = Harness(tool, {}, agent=agent)
    asyncio.run(h.sessions.append_event(h.session, _state_event(session_state)))
    h.session = asyncio.run(h.sessions.get_session(app_name="dak_agent", user_id="u", session_id=h.session.id))

    with patch.object(McpToolset, "get_tools", get_tools), \
            patch("dak_agent.remote_tools.discover_remote_tools", return_value={}):
        events = h.say("write")

    assert built and all(t._require_confirmation is False for t in built)
    assert tool.calls == []
    assert [fc for e in events for fc in e.get_function_calls() if fc.name == "adk_request_confirmation"]


def test_tool_source(monkeypatch):
    assert tool_source(mcp_tool("read_file"), DEFAULT_URL) == "default"
    assert tool_source(mcp_tool("read_file", "http://caller:9000/mcp"), DEFAULT_URL, {"http://caller:9000/mcp"}) == "caller"
    assert tool_source(mcp_tool("read_file", "http://other:9000/mcp"), DEFAULT_URL) == "http://other:9000/mcp"
    assert tool_source(FunctionTool(lambda: None), DEFAULT_URL) == "local"

    monkeypatch.setenv("DAK_ALLOWED_MCP_URLS", "http://caller:9000/mcp")
    tool = mcp_tool("write_file", "http://caller:9000/mcp")
    Harness(tool, {"name": "write_file", "args": {"path": "a"}}).say("write")
    assert tool.calls == [{"path": "a"}]  # the caller's tools run without confirmation (#136)


def test_config_rules_override_defaults(tmp_path):
    config = tmp_path / "agent_config.yaml"
    config.write_text(
        "permissions:\n"
        "  - {source: default, tool: run_command, pattern: 'pytest *', action: allow}\n"
        "  - {source: default, tool: read_file, action: deny}\n"
    )
    rules = DEFAULT_RULES + load_rules(load_agent_config(str(config)).permission_rules)

    assert len(rules) == len(DEFAULT_RULES) + 2
    assert run("pytest -q", rules) == "allow"
    assert run("pytest -q", DEFAULT_RULES) == "ask"
    assert evaluate(rules, "default", "read_file", {"path": "README.md"}) == "deny"


@pytest.mark.parametrize("entry", [{"tool": "write_file", "action": "sometimes"}, {"action": "deny"}, "deny"])
def test_malformed_config_rule_stops_startup(entry):
    with pytest.raises(ValueError):
        load_rules([entry])


# --- Always approvals and the Ulysses Pact (#178) ---

def test_evaluate_does_not_mutate_state():
    state = {}
    evaluate(DEFAULT_RULES, "default", "write_file", {"path": "a"}, always_approvals(state))
    evaluate_ruleset(DEFAULT_RULES, "default", "write_file", "a")
    assert PERSISTED_ALLOW_KEY not in state


def test_always_does_not_override_deny_or_new_segments():
    state = {}
    record_always_approval(state, "default", "run_command", {"command": "make build"})
    record_always_approval(state, "default", "run_command", {"command": "make build"})  # no duplicate
    assert state[PERSISTED_ALLOW_KEY] == [{"source": "default", "tool": "run_command", "pattern": "make build"}]
    record_always_approval(state, "default", "run_command", {"command": "rm -rf /"})
    record_always_approval(state, "default", "run_command", {"command": "make build > out"})  # not remembered
    assert len(state[PERSISTED_ALLOW_KEY]) == 2
    always = always_approvals(state)

    def run_(command):
        return evaluate(DEFAULT_RULES, "default", "run_command", {"command": command}, always)

    assert run_("make build") == "allow"
    assert run_("make build && rm x") == "ask"  # an extended command asks again
    assert run_("make  build") == "ask"  # another text, even if the shell reads it the same
    assert run_("make build && rm -rf /") == "deny"
    assert run_("make build > out") == "ask"  # never allow what cannot be split
    # Quoting is part of the approval: `rm '*.txt'` does not approve `rm *.txt`.
    quoted = {}
    record_always_approval(quoted, "default", "run_command", {"command": "rm '*.txt'"})
    assert evaluate(DEFAULT_RULES, "default", "run_command", {"command": "rm *.txt"}, always_approvals(quoted)) == "ask"
    assert evaluate(DEFAULT_RULES, "default", "run_command", {"command": "rm '*.txt'"}, always_approvals(quoted)) == "allow"
    # Scoped to the source: another server's run_command still asks.
    assert evaluate(DEFAULT_RULES, "http://other/mcp", "run_command", {"command": "make build"}, always) == "ask"


def test_always_answer_persists_and_skips_next_confirmation():
    for mode, asks_again in (("always", False), ("once", True)):
        tool = mcp_tool("write_file")
        h = Harness(tool, {"name": "write_file", "args": {"path": "a.txt"}})
        h.answer(h.say("write"), True, {"mode": mode, "reason": ""})
        assert tool.calls == [{"path": "a.txt"}]

        h.llm.requests.clear()  # the model calls write_file again in the next turn
        events = h.say("again")
        asked = [fc for e in events for fc in e.get_function_calls() if fc.name == "adk_request_confirmation"]
        assert bool(asked) is asks_again, mode
        assert len(tool.calls) == (1 if asks_again else 2)


def test_always_rule_does_not_bypass_active_plan():
    tool = mcp_tool("run_command")
    h = Harness(tool, {"name": "run_command", "args": {"command": "git status"}})
    asyncio.run(h.sessions.append_event(h.session, _state_event({
        enforcer.PLAN_KEY: ["read_file"],
        PERSISTED_ALLOW_KEY: [{"source": "default", "tool": "run_command", "pattern": "git status"}],
    })))
    h.session = asyncio.run(h.sessions.get_session(app_name="dak_agent", user_id="u", session_id=h.session.id))

    events = h.say("status?")

    assert tool.calls == []
    denied = responses(events, "run_command")[0]
    assert denied["observation"] == "denied_by_policy"
    assert "plan" in denied["reason"]


# --- switch_mode goes through the same judgement (#410) ---

def _switch_mode_run(rules, confirm=None):
    """The model calls switch_mode once; returns (harness, events, final state)."""
    from dak_agent.builtin_tools import switch_mode

    llm = ScriptedLlm(model="scripted", call={"name": "switch_mode", "args": {"reason": "r", "new_focus": "files"}},
                      requests=[])
    agent = AdaptiveAgent(model=llm, name="dak_agent", instruction="Base.", tools=[FunctionTool(switch_mode)])
    h = Harness(None, {}, rules=rules, agent=agent)
    with patch("dak_agent.remote_tools.discover_remote_tools", return_value={}), \
            patch("dak_agent.mode_manager.ModeManager.generate_mode_config",
                  return_value=("Switched instruction.", [], [])) as generate:
        events = h.say("switch")
        if confirm is not None:
            events = h.answer(events, confirm)
    state = asyncio.run(h.sessions.get_session(app_name="dak_agent", user_id="u", session_id=h.session.id)).state
    return events, state, generate


def test_switch_mode_is_applied_when_allowed():
    events, state, generate = _switch_mode_run(DEFAULT_RULES)
    generate.assert_called_once()
    assert state.get(STATE_MODE_INSTRUCTION) == "Switched instruction."


def test_denied_switch_mode_does_not_switch():
    events, state, generate = _switch_mode_run(DEFAULT_RULES + [Rule("local", "switch_mode", "*", "deny")])
    generate.assert_not_called()
    assert STATE_MODE_INSTRUCTION not in state
    assert responses(events, "switch_mode")[0]["observation"] == "denied_by_policy"


def test_switch_mode_waits_for_confirmation():
    ask = DEFAULT_RULES + [Rule("local", "switch_mode", "*", "ask")]
    _, held, generate = _switch_mode_run(ask)
    generate.assert_not_called()  # held: nothing switched yet
    assert STATE_MODE_INSTRUCTION not in held

    _, approved, generate = _switch_mode_run(ask, confirm=True)
    generate.assert_called_once()
    assert approved.get(STATE_MODE_INSTRUCTION) == "Switched instruction."

    _, rejected, generate = _switch_mode_run(ask, confirm=False)
    generate.assert_not_called()
    assert STATE_MODE_INSTRUCTION not in rejected
