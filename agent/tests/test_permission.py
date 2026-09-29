"""Tests for the declarative tool permission rules (dak_agent.permission)."""
import pytest

from dak_agent.permission import (
    DEFAULT_RULES,
    Rule,
    evaluate,
    evaluate_ruleset,
    split_command_segments,
    strictest,
)


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
