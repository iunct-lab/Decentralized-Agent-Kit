"""Declarative tool permission rules: allow / ask / deny.

A rule is keyed by where the tool comes from (`source`), the tool name and a
pattern over the call's main argument (`subject`); all three are globs.
Within one rule list the LAST matching rule wins, so later lists (operator
config) override earlier ones (the defaults). A compound `run_command`
(`a && b | c`) is a different level: each segment is evaluated on its own and
the STRICTEST result wins for the whole command (deny > ask > allow).

`source` is one of:
- `local`: a tool running inside the agent process (built-in / skill tools)
- `default`: the default MCP server (`MCP_SERVER_URL`)
- `caller`: an MCP server the caller passed with `dak:tools`
- any other MCP server: its URL
"""
import fnmatch
import os
import shlex
from dataclasses import dataclass
from typing import Any, Collection, List, Literal, Mapping, Optional

from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.mcp_tool.mcp_tool import McpTool

from .call_config import ALLOWED_MCP_URLS_ENV


Action = Literal["allow", "ask", "deny"]

_SEVERITY = {"allow": 0, "ask": 1, "deny": 2}

# Tools whose `path` argument is what a rule's pattern is matched against.
_PATH_TOOLS = {"read_file", "write_file", "edit_file", "list_files", "search_files", "grep"}

_SEPARATORS = {"&&", "||", ";", "|", "&"}


@dataclass(frozen=True)
class Rule:
    source: str
    tool: str
    pattern: str
    action: Action


def subject(tool_name: str, args: Mapping[str, Any]) -> str:
    """The argument a rule's pattern is matched against ("" when none)."""
    if tool_name == "run_command":
        return str(args.get("command", ""))
    if tool_name in _PATH_TOOLS:
        return str(args.get("path", "."))
    return ""


def strictest(*actions: Action) -> Action:
    return max(actions, key=_SEVERITY.__getitem__)


def evaluate_ruleset(rules: List[Rule], source: str, tool_name: str, value: str) -> Action:
    """The last rule matching all of source / tool / value; "ask" if none."""
    action: Action = "ask"
    for rule in rules:
        if (
            fnmatch.fnmatchcase(source, rule.source)
            and fnmatch.fnmatchcase(tool_name, rule.tool)
            and fnmatch.fnmatchcase(value, rule.pattern)
        ):
            action = rule.action
    return action


def split_command_segments(command: str) -> Optional[List[str]]:
    """Split a shell command on `&&` `||` `;` `|` `&`. None when it cannot be
    judged segment by segment: command substitution, redirection, subshells,
    newlines or unbalanced quotes (what runs is not what the segments say)."""
    if "`" in command or "$(" in command or "\n" in command or "\r" in command:
        return None
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    # shlex would treat `#` anywhere as a comment; the shell only at the start
    # of a word (`git status a#; rm -rf /` runs the rm). Keep `#` as text.
    lexer.commenters = ""
    try:
        tokens = list(lexer)
    except ValueError:
        return None
    segments: List[str] = []
    current: List[str] = []
    for token in tokens:
        if token in _SEPARATORS:
            segments.append(shlex.join(current))
            current = []
        elif token and all(c in lexer.punctuation_chars for c in token):
            return None  # `>`, `<`, `(`, `)`, `>>`, `;;`, ...
        else:
            current.append(token)
    segments.append(shlex.join(current))
    return [s for s in segments if s]


def evaluate(rules: List[Rule], source: str, tool_name: str, args: Mapping[str, Any]) -> Action:
    value = subject(tool_name, args)
    if tool_name != "run_command":
        return evaluate_ruleset(rules, source, tool_name, value)
    segments = split_command_segments(value)
    if segments is None:
        # A deny still applies to the whole text, but it is never allowed.
        return strictest("ask", evaluate_ruleset(rules, source, tool_name, value))
    if not segments:
        return evaluate_ruleset(rules, source, tool_name, value)
    return strictest(*(evaluate_ruleset(rules, source, tool_name, s) for s in segments))


_READ_ONLY_GIT = ("status", "log", "diff", "show")

DEFAULT_RULES: List[Rule] = [
    # Tools inside the agent process: unchanged behaviour (no confirmation).
    Rule("local", "*", "*", "allow"),
    # Tools the caller chose with `dak:tools` run without confirmation (#136).
    Rule("caller", "*", "*", "allow"),
    # The default MCP server: ask, except reads and read-only git.
    Rule("default", "*", "*", "ask"),
    *(Rule("default", t, "*", "allow") for t in ("read_file", "list_files", "search_files", "grep", "deep_think")),
    *(Rule("default", t, p, "ask") for t in ("read_file", "grep") for p in ("*.env", "*.env.*")),
    *(Rule("default", "run_command", p, "allow") for g in _READ_ONLY_GIT for p in (f"git {g}", f"git {g} *")),
    # Options that make those git commands write a file or run a program.
    # No space before the option: shlex.join may quote the word (`'--output=a b'`).
    *(Rule("default", "run_command", f"git *{o}*", "ask") for o in ("--output", "--ext-diff", "--textconv")),
    # Brace, glob, variable and tilde expansion: the shell runs another text
    # than the rule saw (`git diff --outpu{t,t}=f` becomes `--output=f`).
    *(Rule("default", "run_command", p, "ask")
      for p in ("git *[[{}*?$]*", "git * ~*", "git *=~*", "git *:~*", "git *'~*")),
    *(Rule("default", "run_command", p, "deny")
      for p in ("rm -rf *", "rm -fr *", "git push --force*", "git push -f*", "git push * --force*", "git push * -f*")),
]


def tool_source(tool: Any, default_mcp_url: str, caller_urls: Collection[str] = ()) -> str:
    """Where a tool comes from, as a rule's `source` (see the module doc).
    An MCP tool whose server URL cannot be read gets "mcp:unknown", which no
    default rule matches (so it asks)."""
    if not isinstance(tool, McpTool):
        return "local"
    try:
        url = tool._mcp_session_manager._connection_params.url
    except AttributeError:
        return "mcp:unknown"
    if url == default_mcp_url:
        return "default"
    if url in caller_urls:
        return "caller"
    return str(url)


def matching_rules(rules: List[Rule], source: str, tool_name: str, args: Mapping[str, Any]) -> List[str]:
    """The rules that decided a denial, for the Observation."""
    value = subject(tool_name, args)
    values = split_command_segments(value) if tool_name == "run_command" else None
    candidates = (values or []) + [value]
    return [
        f"{r.source} {r.tool} {r.pattern!r} -> {r.action}"
        for r in rules
        if r.action == "deny"
        and fnmatch.fnmatchcase(source, r.source)
        and fnmatch.fnmatchcase(tool_name, r.tool)
        and any(fnmatch.fnmatchcase(v, r.pattern) for v in candidates)
    ]


class PermissionPlugin(BasePlugin):
    """App-wide allow / ask / deny for every tool call, whichever toolset
    (default, skill, mode switch, `dak:tools`) it came from
    (docs/design/permission-boundary.md, decision 1)."""

    def __init__(self, rules: List[Rule], default_mcp_url: str, name: str = "dak_permission"):
        super().__init__(name=name)
        self.rules = list(rules)
        self._default_mcp_url = default_mcp_url

    def _source(self, tool: Any) -> str:
        caller_urls = {u.strip() for u in os.environ.get(ALLOWED_MCP_URLS_ENV, "").split(",") if u.strip()}
        return tool_source(tool, self._default_mcp_url, caller_urls)

    async def before_tool_callback(self, *, tool, tool_args, tool_context) -> Optional[dict]:
        source = self._source(tool)
        action = evaluate(self.rules, source, tool.name, tool_args)
        if action == "allow":
            return None
        if action == "deny":
            return {
                "observation": "denied_by_policy",
                "tool": tool.name,
                "reason": f"'{tool.name}' is denied by policy for this input.",
                "rules": matching_rules(self.rules, source, tool.name, tool_args),
                "hint": "Do not retry the same call. Choose another way, or ask the user to run it themselves.",
            }
        # ask: the same call comes back here with the user's answer.
        confirmation = tool_context.tool_confirmation
        if confirmation is None:
            tool_context.request_confirmation(
                hint=f"Please approve or reject the tool call {tool.name}() by responding with a"
                     " FunctionResponse with an expected ToolConfirmation payload."
            )
            return {"error": "This tool call requires confirmation, please approve or reject."}
        if not confirmation.confirmed:
            # Same text as ADK's tools; the rejection reason is restored from
            # the answer's payload by the agent's after_tool_callback (#174).
            return {"error": "This tool call is rejected."}
        return None


def load_rules(raw: Any) -> List[Rule]:
    """`permissions:` of agent_config.yaml -> rules. A malformed entry stops
    the agent from starting: skipping it could silently drop a deny."""
    rules: List[Rule] = []
    for entry in raw or []:
        if not isinstance(entry, Mapping) or "tool" not in entry or entry.get("action") not in _SEVERITY:
            raise ValueError(f"Invalid permission rule in agent_config.yaml: {entry!r} "
                             "(needs tool and action: allow | ask | deny)")
        rules.append(Rule(str(entry.get("source", "*")), str(entry["tool"]), str(entry.get("pattern", "*")), entry["action"]))
    return rules
