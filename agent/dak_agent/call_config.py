"""Per-call settings: the `dak:` keys a caller passes with a single call.

A caller can pass them as `/run`'s `state_delta` (they land in the session
state) or as A2A message metadata (ADK's A2A request converter puts that under
`RunConfig.custom_metadata["a2a_metadata"]`). Later PBIs add more `dak:` keys
to this module.
"""
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Tuple

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from jsonschema_specifications import REGISTRY as METASCHEMAS

logger = logging.getLogger(__name__)

DAK_PREFIX = "dak:"
STATE_CALL_INSTRUCTION = "dak:instruction"
STATE_CALL_OUTPUT_SCHEMA = "dak:output_schema"  # JSON Schema (dict)
# Tools for this call. A list of names (or {"names": [...]}): only those
# built-in tools and those names from the default MCP server; [] means no tools
# at all. {"mcp_servers": [{"url", "type": "http"|"sse"}], "names"?: [...]}:
# only the tools of the caller's MCP servers (optionally filtered by name).
STATE_CALL_TOOLS = "dak:tools"
# Operator's allow-list for the caller's MCP servers (comma-separated URLs).
# Unset means no caller may pass one: connecting to caller-chosen URLs from the
# agent container would otherwise reach the operator's internal network.
ALLOWED_MCP_URLS_ENV = "DAK_ALLOWED_MCP_URLS"
MCP_CONNECTION_TYPES = ("http", "sse")
TRANSFER_TOOL = "transfer_to_agent"  # ADK's A2A delegation tool (from sub_agents)
# Written by DAK (not the caller): the caller's MCP servers that could not be
# reached on this call, [{"url", "reason"}]. Cleared when they are reachable.
STATE_TOOLS_ERROR = "dak:tools_error"
STATE_CALL_MODEL = "dak:model"  # LiteLLM model id, e.g. "bedrock/openai.gpt-5.6-luna"
# Operator's allow-list for `dak:model` (comma-separated model ids). Unset
# means no caller may pick a model: callers cannot exceed the operator's
# cost limits unless the operator opens that door explicitly.
ALLOWED_MODELS_ENV = "DAK_ALLOWED_MODELS"
# Limits on one turn (one invocation). The operator's default (env) caps
# them; a caller can only narrow it. 0 or unset means no limit.
STATE_CALL_MAX_LLM_CALLS = "dak:max_llm_calls"
STATE_CALL_MAX_SECONDS = "dak:max_seconds"
STATE_CALL_MAX_OUTPUT_TOKENS = "dak:max_output_tokens"
MAX_LLM_CALLS_ENV = "DAK_MAX_LLM_CALLS"
MAX_SECONDS_ENV = "DAK_MAX_TURN_SECONDS"
MAX_OUTPUT_TOKENS_ENV = "DAK_MAX_OUTPUT_TOKENS"
# Same value as google.adk.a2a.converters.request_converter.A2A_METADATA_KEY.
A2A_METADATA_KEY = "a2a_metadata"


def _dak_keys(source: Any) -> Dict[str, Any]:
    if not isinstance(source, Mapping):
        return {}
    return {k: v for k, v in source.items() if isinstance(k, str) and k.startswith(DAK_PREFIX)}


def resolve_dak_settings(callback_context) -> Dict[str, Any]:
    """Collect this call's `dak:` settings. Precedence (low → high):
    `run_config.custom_metadata`, its nested A2A metadata, session state."""
    try:
        run_config = callback_context._invocation_context.run_config
    except AttributeError:
        run_config = None
    custom_metadata = getattr(run_config, "custom_metadata", None) or {}

    settings: Dict[str, Any] = {}
    settings.update(_dak_keys(custom_metadata))
    if isinstance(custom_metadata, Mapping):
        settings.update(_dak_keys(custom_metadata.get(A2A_METADATA_KEY)))
    state = callback_context.state
    # ADK's `State` is not a Mapping; `to_dict()` merges the committed value
    # with this invocation's pending delta.
    settings.update(_dak_keys(state.to_dict() if hasattr(state, "to_dict") else state))
    return settings


@dataclass(frozen=True)
class CallLimits:
    """This turn's limits; 0 means no limit."""
    max_llm_calls: int = 0
    max_seconds: float = 0.0
    max_output_tokens: int = 0


def _as_limit(raw: Any, cast, source: str) -> float:
    """A positive number, or 0 (no limit) when absent or invalid."""
    if raw is None:
        return 0
    try:
        value = cast(raw)
    except (TypeError, ValueError, OverflowError):  # OverflowError: int(float("inf"))
        value = -1
    if value < 0:
        logger.warning("Ignoring invalid %s=%r; no limit from it.", source, raw)
        return 0
    return value


def _clip_limit(env_name: str, state_key: str, call_settings: Dict[str, Any], cast) -> float:
    """The caller's value, capped by the operator's default. The caller cannot
    lift the operator's limit: 0 (or nothing) from the caller keeps it."""
    operator_default = _as_limit(os.getenv(env_name) or None, cast, env_name)
    caller_value = _as_limit(call_settings.get(state_key), cast, state_key)
    if operator_default > 0:
        return min(operator_default, caller_value) if caller_value > 0 else operator_default
    return caller_value if caller_value > 0 else 0


def resolve_call_limits(call_settings: Dict[str, Any]) -> CallLimits:
    return CallLimits(
        max_llm_calls=int(_clip_limit(MAX_LLM_CALLS_ENV, STATE_CALL_MAX_LLM_CALLS, call_settings, int)),
        max_seconds=float(_clip_limit(MAX_SECONDS_ENV, STATE_CALL_MAX_SECONDS, call_settings, float)),
        max_output_tokens=int(_clip_limit(MAX_OUTPUT_TOKENS_ENV, STATE_CALL_MAX_OUTPUT_TOKENS, call_settings, int)),
    )


def resolve_allowed_models() -> Optional[FrozenSet[str]]:
    """The operator's allow-list, or None when `DAK_ALLOWED_MODELS` is unset."""
    raw = os.environ.get(ALLOWED_MODELS_ENV)
    if raw is None:
        return None
    return frozenset(m.strip() for m in raw.split(",") if m.strip())


def resolve_model_selection(
    call_settings: Dict[str, Any], default_model_name: str
) -> Tuple[str, Optional[Dict[str, Any]]]:
    """The model this call runs on, and an error dict when the caller asked
    for a model the operator does not allow (the call must then not reach
    any LLM)."""
    requested = call_settings.get(STATE_CALL_MODEL)
    if requested is None:
        return default_model_name, None
    allowed = resolve_allowed_models()
    if allowed is None or not isinstance(requested, str) or requested not in allowed:
        return default_model_name, {
            "error": "model_not_allowed",
            "requested_model": requested,
            "allowed_models": sorted(allowed or []),
        }
    return requested, None


def _issue_path(error) -> str:
    """"/"-joined path of the offending value. A missing required property is
    reported by jsonschema on its parent object; name the property itself."""
    path = [str(p) for p in error.absolute_path]
    if error.validator == "required":
        missing = [p for p in error.validator_value if error.message.startswith(repr(p))]
        path += missing[:1]
    return "/".join(path)


def _reject_constant(name: str):
    """Python's json accepts NaN/Infinity; standard JSON does not."""
    raise ValueError(f"{name} is not valid JSON")


def validate_call_output(schema: Dict[str, Any], text: str) -> Tuple[Optional[Any], List[Dict[str, str]]]:
    """Check a final model reply against the call's `dak:output_schema`.
    Returns `(parsed_json, [])` on success, `(None, issues)` otherwise, each
    issue being `{"path": "a/b", "message": "..."}`."""
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        return None, [{"path": "", "message": f"invalid output_schema: {exc.message}"}]
    try:
        parsed = json.loads(text, parse_constant=_reject_constant)
    except (ValueError, RecursionError) as exc:  # RecursionError: absurdly deep nesting
        return None, [{"path": "", "message": f"invalid JSON: {exc}"}]
    # The schema comes from the caller. jsonschema's default registry fetches
    # remote `$ref` URLs; this one only knows the bundled metaschemas, so a
    # remote ref is unresolvable instead of a request from this container.
    validator = Draft202012Validator(schema, registry=METASCHEMAS)
    try:
        errors = sorted(validator.iter_errors(parsed), key=lambda e: [str(p) for p in e.absolute_path])
    except Exception as exc:  # unresolvable $ref and the like
        return None, [{"path": "", "message": f"invalid output_schema: {exc}"}]
    if errors:
        return None, [{"path": _issue_path(e), "message": e.message} for e in errors]
    return parsed, []


def call_tool_names(value: Any) -> Optional[List[str]]:
    """The names in `dak:tools` (list form, or the dict's "names"); None when
    the dict form gives none."""
    if isinstance(value, list):
        return value
    if isinstance(value, Mapping):
        return value.get("names")
    return None


def validate_call_tools(call_settings: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """An error dict when `dak:tools` is present but malformed, or names MCP
    servers the operator does not allow. A caller who asked for a restriction
    must not silently get every tool."""
    value = call_settings.get(STATE_CALL_TOOLS)
    if value is None:
        return None
    names = call_tool_names(value)
    shape_ok = (
        (isinstance(value, list) or (isinstance(value, Mapping) and value and set(value) <= {"names", "mcp_servers"}))
        and (names is None or (isinstance(names, list) and all(isinstance(n, str) for n in names)))
    )
    if not shape_ok:
        return {"error": "invalid_tools",
                "expected": 'a list of tool names, e.g. ["read_file"] ([] for no tools), '
                            'or {"mcp_servers": [{"url": "...", "type": "http"|"sse"}], "names"?: [...]}'}
    return resolve_caller_mcp_servers(call_settings)[1]


def resolve_caller_mcp_servers(
    call_settings: Dict[str, Any],
) -> Tuple[Optional[List[Dict[str, str]]], Optional[Dict[str, Any]]]:
    """The caller's MCP servers from `dak:tools` ({"mcp_servers": [...]}),
    normalized to [{"url", "type"}], or an error dict when they are malformed
    or not in the operator's allow-list (the call must then not reach any
    LLM). (None, None) when the call does not name MCP servers."""
    value = call_settings.get(STATE_CALL_TOOLS)
    if not isinstance(value, Mapping) or "mcp_servers" not in value:
        return None, None
    entries = value.get("mcp_servers")
    servers = []
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, Mapping) or not isinstance(entry.get("url"), str):
                break
            conn_type = entry.get("type", "http")
            if conn_type not in MCP_CONNECTION_TYPES:
                break
            server = {"url": entry["url"].strip(), "type": conn_type}
            if server not in servers:
                servers.append(server)
        else:
            entries = None  # all valid
    if entries is not None:
        return None, {"error": "invalid_mcp_servers",
                      "expected": '{"mcp_servers": [{"url": "...", "type": "http"|"sse"}]}'}

    raw = os.environ.get(ALLOWED_MCP_URLS_ENV)
    allowed = frozenset(u.strip() for u in (raw or "").split(",") if u.strip())
    refused = [s["url"] for s in servers if s["url"] not in allowed]
    if refused:
        return None, {"error": "mcp_server_not_allowed", "requested_urls": refused,
                      "allowed_urls": sorted(allowed)}
    return servers, None
