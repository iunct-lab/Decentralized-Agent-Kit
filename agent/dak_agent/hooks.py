"""External hooks declared in `DAK_HOOKS`: command / http, Claude Code's JSON contract.

`DAK_HOOKS` is a JSON array; each entry is
`{"event": "PreToolUse"|"PostToolUse"|"Stop", "type": "command"|"http", "command"|"url": ..., "timeout": seconds, "if": "<tool glob>"}`.
Unset or empty means no hooks. A malformed entry is skipped with a warning.

A hook gets the Claude Code input on stdin (command) or as the POST body (http).
A command hook answers with its exit code: 2 blocks (stderr is the reason), 0
reads an optional `hookSpecificOutput` JSON from stdout, anything else is an
error. An http hook answers with the same JSON in a 2xx body.

Every hook's answer becomes one outcome:
`{"decision": "allow"|"deny"|"error", "reason": str, "updated_input": dict|None, "updated_output": Any|None}`.
"""
import fnmatch
import json
import logging
import os
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

HOOKS_ENV = "DAK_HOOKS"


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
        if not isinstance(entry, dict) or "event" not in entry or "type" not in entry:
            logger.warning("Ignoring hook in %s: %r (needs event and type).", HOOKS_ENV, entry)
            continue
        try:
            specs.append(HookSpec(
                event=str(entry["event"]),
                type=str(entry["type"]),
                command=entry.get("command"),
                url=entry.get("url"),
                timeout=float(entry.get("timeout", 30.0)),
                tool_pattern=entry.get("if"),
            ))
        except (TypeError, ValueError):
            logger.warning("Ignoring hook in %s: %r (timeout must be a number).", HOOKS_ENV, entry)
    return specs


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


def _outcome(decision: str, reason: str = "", updated_input: Optional[dict] = None,
             updated_output: Any = None) -> Dict[str, Any]:
    return {"decision": decision, "reason": reason, "updated_input": updated_input, "updated_output": updated_output}


def _decision_from_stdout(text: str) -> Dict[str, Any]:
    try:
        parsed = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    output = parsed.get("hookSpecificOutput", {})
    return output if isinstance(output, dict) else {}


def _from_output(output: Dict[str, Any]) -> Dict[str, Any]:
    updated_input = output.get("updatedInput")
    return _outcome(
        "deny" if output.get("permissionDecision") == "deny" else "allow",
        str(output.get("permissionDecisionReason") or ""),
        updated_input if isinstance(updated_input, dict) else None,
        output.get("updatedToolOutput"),
    )


def run_command_hook(hook: HookSpec, payload: Dict[str, Any]) -> Dict[str, Any]:
    try:
        proc = subprocess.run(hook.command or "", shell=True, input=json.dumps(payload, default=str),
                              capture_output=True, text=True, timeout=hook.timeout)
    except subprocess.TimeoutExpired:
        return _outcome("error", f"hook timed out after {hook.timeout}s: {hook.command}")
    if proc.returncode == 2:
        return _outcome("deny", proc.stderr.strip())
    if proc.returncode != 0:
        return _outcome("error", f"hook exited {proc.returncode}: {proc.stderr.strip()}")
    return _from_output(_decision_from_stdout(proc.stdout))


def run_http_hook(hook: HookSpec, payload: Dict[str, Any]) -> Dict[str, Any]:
    req = urllib.request.Request(hook.url or "", data=json.dumps(payload, default=str).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=hook.timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except TimeoutError:
        return _outcome("error", f"hook timed out after {hook.timeout}s: {hook.url}")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return _outcome("error", f"hook request to {hook.url} failed: {exc}")
    return _from_output(_decision_from_stdout(body))


def run_hook(hook: HookSpec, payload: Dict[str, Any]) -> Dict[str, Any]:
    if hook.type == "command":
        return run_command_hook(hook, payload)
    if hook.type == "http":
        return run_http_hook(hook, payload)
    return _outcome("error", f"unknown hook type '{hook.type}'")
