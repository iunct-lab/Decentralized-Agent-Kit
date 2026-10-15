"""One `request` line per HTTP request, on stderr (docs/architecture/overview.md).

Only a fixed set of fields is read from the path, the query and (for `/run`,
`/run_sse`, `/approvals/{id}/reply`) the body: never the prompt, the state, a
tool's arguments or an answer.
"""
import json
import logging
import re
from typing import Any, Mapping

logger = logging.getLogger(__name__)
# Uvicorn configures only its own loggers; like the tool_call line, this goes to
# stderr on its own and only once.
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False

MODES = {"once", "always", "reject"}
BODY_PATHS = {"/run", "/run_sse"}
_APPROVAL_REPLY = re.compile(r"^/approvals/([^/]+)/reply$")
_SESSION_PATH = re.compile(r"^/apps/([^/]+)/users/([^/]+)(?:/sessions/([^/]+))?")
_IDS = ("app_name", "user_id", "session_id")
_ORDER = ("client", "method", "path", "status", "ms", *_IDS, "approval_id", "mode", "trace_id")
# The caller decides these; they are quoted so a newline or `"` cannot forge a line or a field.
_QUOTED = {"path", *_IDS, "approval_id"}


def reads_body(method: str, path: str) -> bool:
    return method == "POST" and (path in BODY_PATHS or bool(_APPROVAL_REPLY.match(path)))


def request_fields(path: str, query: Mapping[str, str], body: Any) -> dict[str, str]:
    fields: dict[str, str] = {}
    if m := _SESSION_PATH.match(path):
        fields.update({k: v for k, v in zip(_IDS, m.groups()) if v})
    # The body wins over the query: the POST routes act on the body's ids.
    if isinstance(body, dict):
        for key in _IDS:
            if key not in fields and isinstance(body.get(key), str):
                fields[key] = body[key]
    for key in _IDS:
        if key not in fields and key in query:
            fields[key] = query[key]
    if m := _APPROVAL_REPLY.match(path):
        fields["approval_id"] = m.group(1)
        if isinstance(body, dict) and isinstance(body.get("mode"), str) and body["mode"] in MODES:
            fields["mode"] = body["mode"]
    return fields


def format_line(fields: Mapping[str, Any]) -> str:
    parts = []
    for key in _ORDER:
        value = fields.get(key)
        if value is None or value == "":
            text = "-"
        elif key in _QUOTED:
            text = json.dumps(str(value))
        else:
            text = str(value)
        parts.append(f"{key}={text}")
    return "request " + " ".join(parts)
