"""Tests for the external hooks declared in DAK_HOOKS (dak_agent.hooks)."""
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from dak_agent import hooks
from dak_agent.hooks import HookSpec


def test_load_hooks_empty_when_unset(monkeypatch):
    monkeypatch.delenv("DAK_HOOKS", raising=False)
    assert hooks.load_hooks() == []
    monkeypatch.setenv("DAK_HOOKS", "")
    assert hooks.load_hooks() == []


def test_load_hooks_parses_valid_entry(monkeypatch):
    monkeypatch.setenv("DAK_HOOKS", json.dumps([
        {"event": "PreToolUse", "type": "command", "command": "exit 0", "timeout": 5, "if": "run_*"},
        {"event": "Stop", "type": "http", "url": "http://hooks.example/stop"},
    ]))
    assert hooks.load_hooks() == [
        HookSpec(event="PreToolUse", type="command", command="exit 0", timeout=5.0, tool_pattern="run_*"),
        HookSpec(event="Stop", type="http", url="http://hooks.example/stop"),
    ]


def test_load_hooks_skips_invalid_entry_and_logs(monkeypatch, caplog):
    monkeypatch.setenv("DAK_HOOKS", json.dumps([
        {"type": "command", "command": "exit 0"},
        "not a hook",
        {"event": "PostToolUse", "type": "command", "command": "exit 0"},
    ]))
    with caplog.at_level(logging.WARNING, logger="dak_agent.hooks"):
        loaded = hooks.load_hooks()
    assert loaded == [HookSpec(event="PostToolUse", type="command", command="exit 0")]
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2

    monkeypatch.setenv("DAK_HOOKS", "{not json")
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="dak_agent.hooks"):
        assert hooks.load_hooks() == []
    assert caplog.records


def test_hooks_for_filters_by_event_and_pattern():
    any_tool = HookSpec(event="PreToolUse", type="command", command="exit 0")
    run_only = HookSpec(event="PreToolUse", type="command", command="exit 0", tool_pattern="run_*")
    post = HookSpec(event="PostToolUse", type="command", command="exit 0")
    specs = [any_tool, run_only, post]
    assert hooks.hooks_for(specs, "PreToolUse", "run_command") == [any_tool, run_only]
    assert hooks.hooks_for(specs, "PreToolUse", "write_file") == [any_tool]
    assert hooks.hooks_for(specs, "PostToolUse", "write_file") == [post]


def test_build_payload_matches_claude_code_input():
    pre = hooks.build_payload("PreToolUse", "run_command", {"command": "ls"}, session_id="s1", tool_use_id="t1")
    assert pre["hook_event_name"] == "PreToolUse"
    assert pre["session_id"] == "s1"
    assert pre["tool_name"] == "run_command"
    assert pre["tool_input"] == {"command": "ls"}
    assert pre["tool_use_id"] == "t1"
    assert pre["permission_mode"] == "default"
    assert "cwd" in pre
    assert "tool_response" not in pre

    post = hooks.build_payload("PostToolUse", "run_command", {"command": "ls"}, session_id="s1", tool_use_id="t1",
                               result={"stdout": "a"})
    assert post["tool_response"] == {"stdout": "a"}


def _payload():
    return hooks.build_payload("PreToolUse", "run_command", {"command": "ls"}, session_id="s1", tool_use_id="t1")


def _command(cmd, timeout=5.0):
    return HookSpec(event="PreToolUse", type="command", command=cmd, timeout=timeout)


def test_run_command_hook_exit2_is_deny():
    outcome = hooks.run_hook(_command("echo denied >&2; exit 2"), _payload())
    assert outcome["decision"] == "deny"
    assert outcome["reason"] == "denied"


def test_run_command_hook_json_deny_on_exit0():
    out = json.dumps({"hookSpecificOutput": {"permissionDecision": "deny", "permissionDecisionReason": "no"}})
    outcome = hooks.run_hook(_command(f"echo '{out}'"), _payload())
    assert outcome["decision"] == "deny"
    assert outcome["reason"] == "no"


def test_run_command_hook_reads_payload_from_stdin():
    # The hook sees the Claude Code input contract on stdin.
    script = ("python3 -c 'import json,sys; p=json.load(sys.stdin); "
              "print(json.dumps({\"hookSpecificOutput\": {\"permissionDecision\": \"deny\", "
              "\"permissionDecisionReason\": p[\"tool_name\"] + \":\" + p[\"tool_input\"][\"command\"]}}))'")
    outcome = hooks.run_hook(_command(script), _payload())
    assert outcome == {"decision": "deny", "reason": "run_command:ls", "updated_input": None, "updated_output": None}


def test_run_command_hook_updated_input():
    out = json.dumps({"hookSpecificOutput": {"permissionDecision": "allow", "updatedInput": {"command": "ls -la"}}})
    outcome = hooks.run_hook(_command(f"echo '{out}'"), _payload())
    assert outcome["decision"] == "allow"
    assert outcome["updated_input"] == {"command": "ls -la"}
    assert outcome["updated_output"] is None


def test_run_command_hook_updated_output():
    out = json.dumps({"hookSpecificOutput": {"updatedToolOutput": "redacted"}})
    outcome = hooks.run_hook(_command(f"echo '{out}'"), _payload())
    assert outcome["decision"] == "allow"
    assert outcome["updated_output"] == "redacted"


def test_run_command_hook_empty_stdout_is_allow():
    outcome = hooks.run_hook(_command("exit 0"), _payload())
    assert outcome == {"decision": "allow", "reason": "", "updated_input": None, "updated_output": None}


def test_run_command_hook_other_exit_is_error():
    outcome = hooks.run_hook(_command("echo broken >&2; exit 1"), _payload())
    assert outcome["decision"] == "error"
    assert "broken" in outcome["reason"]


def test_run_command_hook_timeout():
    outcome = hooks.run_hook(_command("sleep 5", timeout=0.1), _payload())
    assert outcome["decision"] == "error"
    assert "timed out" in outcome["reason"]


def test_run_hook_unknown_type_is_error():
    outcome = hooks.run_hook(HookSpec(event="PreToolUse", type="prompt"), _payload())
    assert outcome["decision"] == "error"
    assert "unknown hook type 'prompt'" in outcome["reason"]


class _HookHandler(BaseHTTPRequestHandler):
    reply = b""

    def do_POST(self):
        received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(self.reply)

    def log_message(self, *args):
        pass


received: list = []


@pytest.fixture
def hook_server():
    received.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HookHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def test_run_http_hook_posts_payload_and_reads_decision(hook_server):
    _HookHandler.reply = json.dumps(
        {"hookSpecificOutput": {"permissionDecision": "deny", "permissionDecisionReason": "http says no"}}).encode()
    url = f"http://127.0.0.1:{hook_server.server_address[1]}/hook"
    outcome = hooks.run_hook(HookSpec(event="PreToolUse", type="http", url=url), _payload())
    assert outcome["decision"] == "deny"
    assert outcome["reason"] == "http says no"
    assert received[0]["tool_name"] == "run_command"
    assert received[0]["tool_input"] == {"command": "ls"}


def test_run_http_hook_connection_failure_is_error():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HookHandler)
    port = server.server_address[1]
    server.server_close()
    outcome = hooks.run_hook(HookSpec(event="PreToolUse", type="http", url=f"http://127.0.0.1:{port}/"), _payload())
    assert outcome["decision"] == "error"
