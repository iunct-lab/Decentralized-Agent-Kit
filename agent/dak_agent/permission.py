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
import shlex
from dataclasses import dataclass
from typing import Any, List, Literal, Mapping, Optional

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
    *(Rule("default", "run_command", p, "deny")
      for p in ("rm -rf *", "rm -fr *", "git push --force*", "git push -f*", "git push * --force*", "git push * -f*")),
]
