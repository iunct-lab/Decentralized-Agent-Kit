"""External hooks declared in `DAK_HOOKS`: command / http, Claude Code's JSON contract.

`DAK_HOOKS` is a JSON array; each entry is
`{"event": "PreToolUse"|"PostToolUse"|"Stop", "type": "command"|"http", "command"|"url": ..., "timeout": seconds, "if": "<tool glob>"}`.
Unset or empty means no hooks. A malformed entry is skipped with a warning.

A hook gets the Claude Code input on stdin (command) or as the POST body (http).
A command hook answers with its exit code: 2 blocks (stderr is the reason), 0
reads an optional `hookSpecificOutput` JSON from stdout, anything else is an
error. An http hook answers with the same JSON in a 2xx body. A top-level
`{"decision": "block"}` also blocks; `permissionDecision: "ask"` blocks too,
since a hook call has nobody to ask.

Every hook's answer becomes one outcome:
`{"decision": "allow"|"deny"|"error", "reason": str, "updated_input": dict|None, "updated_output": Any|None}`.
"""
import fnmatch
import http.client
import json
import logging
import os
import signal
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

HOOKS_ENV = "DAK_HOOKS"
# The longest wait every path accepts: a command hook's wait polls in C-int milliseconds (about 24.8 days).
MAX_TIMEOUT = (2**31 - 1) // 1000


@dataclass(frozen=True)
class HookSpec:
    event: str
    type: str
    command: Optional[str] = None
    url: Optional[str] = None
    timeout: float = 30.0
    tool_pattern: Optional[str] = None


def load_hooks() -> List[HookSpec]:
    raw = os.getenv(HOOKS_ENV)
    if not raw:
        return []
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("Ignoring %s: not valid JSON (%s).", HOOKS_ENV, exc)
        return []
    if not isinstance(entries, list):
        logger.warning("Ignoring %s: expected a JSON array.", HOOKS_ENV)
        return []
    specs: List[HookSpec] = []
    for entry in entries:
        problem = _entry_problem(entry)
        if problem:
            logger.warning("Ignoring hook in %s: %r (%s).", HOOKS_ENV, entry, problem)
            continue
        specs.append(HookSpec(
            event=entry["event"],
            type=entry["type"],
            command=entry.get("command"),
            url=entry.get("url"),
            timeout=float(entry.get("timeout", 30.0)),
            tool_pattern=entry.get("if"),
        ))
    return specs


def _entry_problem(entry: Any) -> str:
    if not isinstance(entry, dict) or not isinstance(entry.get("event"), str) or not isinstance(entry.get("type"), str):
        return "needs event and type"
    for key in ("command", "url", "if"):
        if key in entry and not isinstance(entry[key], str):
            return f"{key} must be a string"
    if entry["type"] == "command" and not entry.get("command"):
        return "a command hook needs command"
    if entry["type"] == "http" and not entry.get("url", "").lower().startswith(("http://", "https://")):
        return "an http hook needs an http(s) url"
    timeout = entry.get("timeout", 30.0)
    # Compared, not converted: an integer too large for a float does not raise here. NaN fails both.
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= MAX_TIMEOUT:
        return "timeout must be a positive number a wait accepts"
    return ""


def hooks_for(hooks: List[HookSpec], event: str, tool_name: str) -> List[HookSpec]:
    return [h for h in hooks
            if h.event == event and (h.tool_pattern is None or fnmatch.fnmatch(tool_name, h.tool_pattern))]


def build_payload(event: str, tool_name: str, tool_args: Dict[str, Any], *, session_id: str, tool_use_id: str,
                  result: Any = None) -> Dict[str, Any]:
    payload = {
        "hook_event_name": event,
        "session_id": session_id,
        "cwd": os.getcwd(),
        "permission_mode": "default",
        "tool_name": tool_name,
        "tool_input": tool_args,
        "tool_use_id": tool_use_id,
    }
    if result is not None:
        payload["tool_response"] = result
    return payload


def _label(hook: HookSpec) -> str:
    """How a failure names the hook. The reason reaches the model and the session, so it leaves out the
    command line and the URL's credentials, path and query, where a secret may sit."""
    if hook.type == "http":
        try:
            url = urllib.parse.urlsplit(hook.url or "")
        except ValueError:  # e.g. an unclosed IPv6 bracket
            return f"{hook.event} http hook"
        return f"{hook.event} http hook {url.scheme}://{url.netloc.rpartition('@')[2]}"
    return f"{hook.event} {hook.type} hook"


def _outcome(decision: str, reason: str = "", updated_input: Optional[dict] = None,
             updated_output: Any = None) -> Dict[str, Any]:
    return {"decision": decision, "reason": reason, "updated_input": updated_input, "updated_output": updated_output}


def _decision_from_stdout(text: str) -> Dict[str, Any]:
    """`hookSpecificOutput`, with Claude Code's top-level `{"decision": "block", "reason": ...}`
    (how its PostToolUse / Stop hooks block) folded in as a deny."""
    try:
        parsed = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    output = parsed.get("hookSpecificOutput", {})
    output = dict(output) if isinstance(output, dict) else {}
    if parsed.get("decision") == "block" and "permissionDecision" not in output:
        output["permissionDecision"] = "deny"
        output["permissionDecisionReason"] = parsed.get("reason", "")
    return output


def _from_output(output: Dict[str, Any]) -> Dict[str, Any]:
    decision = output.get("permissionDecision")
    reason = str(output.get("permissionDecisionReason") or "")
    if decision not in (None, "allow", "deny", "ask"):
        logger.warning("Hook returned unknown permissionDecision %r; treating it as allow.", decision)
    if decision == "ask":
        # Nobody can be asked from inside a hook call here; not running the tool is the safe answer.
        decision, reason = "deny", f"hook asked for confirmation, which DAK hooks do not support: {reason}"
    updated_input = output.get("updatedInput")
    return _outcome(
        "deny" if decision == "deny" else "allow",
        reason,
        updated_input if isinstance(updated_input, dict) else None,
        output.get("updatedToolOutput"),
    )


def run_command_hook(hook: HookSpec, payload: Dict[str, Any]) -> Dict[str, Any]:
    # Its own process group, so a timeout also stops whatever the hook started.
    proc = subprocess.Popen(hook.command or "", shell=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        stdout, stderr = proc.communicate(json.dumps(payload, default=str), timeout=hook.timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate()
        return _outcome("error", f"hook timed out after {hook.timeout}s: {_label(hook)}")
    if proc.returncode == 2:
        return _outcome("deny", stderr.strip())
    if proc.returncode != 0:
        return _outcome("error", f"hook exited {proc.returncode}: {stderr.strip()}")
    return _from_output(_decision_from_stdout(stdout))


def run_http_hook(hook: HookSpec, payload: Dict[str, Any]) -> Dict[str, Any]:
    try:
        req = urllib.request.Request(hook.url or "", data=json.dumps(payload, default=str).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=hook.timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except TimeoutError:
        return _outcome("error", f"hook timed out after {hook.timeout}s: {_label(hook)}")
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            return _outcome("error", f"hook timed out after {hook.timeout}s: {_label(hook)}")
        return _outcome("error", f"{_label(hook)} failed: {exc}")
    except OSError as exc:
        return _outcome("error", f"{_label(hook)} failed: {exc}")
    except (ValueError, http.client.HTTPException) as exc:  # bad URLs; their messages may quote the URL
        return _outcome("error", f"{_label(hook)} failed: {type(exc).__name__}")
    return _from_output(_decision_from_stdout(body))


def run_hook(hook: HookSpec, payload: Dict[str, Any]) -> Dict[str, Any]:
    if hook.type == "command":
        return run_command_hook(hook, payload)
    if hook.type == "http":
        return run_http_hook(hook, payload)
    return _outcome("error", f"unknown hook type '{hook.type}'")
