"""Pending approvals, read from a session's events (docs/design/approval-queue.md).

ADK already keeps a tool confirmation as an `adk_request_confirmation`
function call in the session, and any client that posts the matching
function response to `/run` resumes it. So nothing is stored here: the list
is rebuilt from the events every time, and a reply is just the `new_message`
to send.

`events` are ADK events dumped as JSON
(`event.model_dump(mode="json", by_alias=True, exclude_none=True)`), the same
shape `GET /apps/{app}/users/{user}/sessions/{session}` returns.
"""
import os
import time

REQUEST_CONFIRMATION = "adk_request_confirmation"
ASK_QUESTION = "ask_question"
REPLY_MODES = ("once", "always", "reject", "timed_out")

PENDING_TIMEOUT_SECONDS = float(os.getenv("DAK_APPROVAL_TIMEOUT_SECONDS", "900"))


def _parts(event: dict) -> list[dict]:
    return (event.get("content") or {}).get("parts") or []


def _is_compaction(event: dict) -> bool:
    """A compaction event (ADK's, or harness._compaction_event) is authored
    "user" but carries only `actions.compaction`: it is not an answer."""
    return bool((event.get("actions") or {}).get("compaction")) and not _parts(event)


def _since_last_user_event(events: list[dict]) -> list[dict]:
    """ADK only reads a confirmation from the last user event, so anything
    asked before it was either answered or abandoned. Compaction events are
    dropped, as they are appended after an invocation that may end on a
    request."""
    events = [e for e in events if not _is_compaction(e)]
    for i in range(len(events) - 1, -1, -1):
        if events[i].get("author") == "user":
            return events[i + 1:]
    return events


def list_pending_approvals(events: list[dict]) -> list[dict]:
    """Confirmation requests after the last user event. An answer is itself a
    user event, so every request after the last one is still unanswered."""
    pending = []
    for event in _since_last_user_event(events):
        for part in _parts(event):
            call = part.get("functionCall")
            if not call or call.get("name") != REQUEST_CONFIRMATION:
                continue
            args = call.get("args") or {}
            original = args.get("originalFunctionCall") or {}
            pending.append({
                "id": call.get("id"),
                "kind": "approval",
                "tool_name": original.get("name"),
                "tool_args": original.get("args") or {},
                "hint": (args.get("toolConfirmation") or {}).get("hint", ""),
                "requested_at": event.get("timestamp"),
            })
    return pending


def list_pending_questions(events: list[dict]) -> list[dict]:
    """`ask_question` calls after the last user event. The tool ends the
    invocation, and the next user message (from any client) is the answer."""
    pending = []
    for event in _since_last_user_event(events):
        for part in _parts(event):
            call = part.get("functionCall")
            if not call or call.get("name") != ASK_QUESTION:
                continue
            args = call.get("args") or {}
            pending.append({
                "id": call.get("id"),
                "kind": "question",
                "tool_name": ASK_QUESTION,
                "questions": args.get("questions") or [],
                "context": args.get("context", ""),
                "requested_at": event.get("timestamp"),
            })
    return pending


def list_pending(events: list[dict]) -> list[dict]:
    """Every pending approval and question, oldest first."""
    return sorted(list_pending_approvals(events) + list_pending_questions(events),
                  key=lambda p: p["requested_at"] or 0)


def build_reply_function_response(fc_id: str, mode: str, reason: str = "") -> dict:
    """The `new_message` that answers a confirmation: `mode` is once / always /
    reject (timed_out when DAK answers an expired one). `confirmed` is always
    set, as the CLI's answer (`cli/src/client.py`) is."""
    if mode not in REPLY_MODES:
        raise ValueError(f"unknown reply mode: {mode!r}")
    return {"parts": [{"functionResponse": {
        "id": fc_id,
        "name": REQUEST_CONFIRMATION,
        "response": {"confirmed": mode in ("once", "always"), "payload": {"mode": mode, "reason": reason}},
    }}]}


def build_question_reply(answer: str) -> dict:
    """The `new_message` that answers a question: a plain user message."""
    return {"parts": [{"text": answer}]}


def is_expired(requested_at: float) -> bool:
    return time.time() - requested_at > PENDING_TIMEOUT_SECONDS
