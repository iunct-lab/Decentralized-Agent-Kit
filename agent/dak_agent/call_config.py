"""Per-call settings: the `dak:` keys a caller passes with a single call.

A caller can pass them as `/run`'s `state_delta` (they land in the session
state) or as A2A message metadata (ADK's A2A request converter puts that under
`RunConfig.custom_metadata["a2a_metadata"]`). Later PBIs add more `dak:` keys
to this module.
"""
import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Tuple

import httpx
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
# Checks a final reply must pass before DAK returns it; a failing reply is
# regenerated with the errors attached. {"json_schema": {...}} and/or
# {"http": {"url", "timeout_seconds"?}} (the caller's endpoint gets the parsed
# reply as a JSON POST and answers {"valid": bool, "errors": [...]}) and/or
# {"mcp": {"url", "tool", "timeout_seconds"?}} (DAK calls that tool of the
# caller's streamable-HTTP MCP server with {"data": <reply>}; its text answers
# the same JSON).
STATE_CALL_INSPECTION = "dak:inspection"
INSPECTION_KINDS = ("json_schema", "http", "mcp")
# Operator's allow-list for the caller's inspection endpoints (comma-separated
# URLs). Unset means none may be called, for the same reason as
# ALLOWED_MCP_URLS_ENV.
ALLOWED_INSPECTION_URLS_ENV = "DAK_ALLOWED_INSPECTION_URLS"
DEFAULT_INSPECTION_RETRIES = 3  # attempts when no max_llm_calls limit applies
INSPECTION_TIMEOUT_S = 10.0
MAX_INSPECTION_TIMEOUT_S = 60.0  # a caller's endpoint cannot hold a turn longer
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


def _schema_error(schema: Dict[str, Any], label: str) -> List[Dict[str, str]]:
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        return [{"path": "", "message": f"invalid {label}: {exc.message}"}]
    return []


def _json_schema_errors(schema: Dict[str, Any], parsed: Any, label: str) -> List[Dict[str, str]]:
    """Issues of `parsed` against `schema`, each `{"path": "a/b", "message": "..."}`."""
    issues = _schema_error(schema, label)
    if issues:
        return issues
    # The schema comes from the caller. jsonschema's default registry fetches
    # remote `$ref` URLs; this one only knows the bundled metaschemas, so a
    # remote ref is unresolvable instead of a request from this container.
    validator = Draft202012Validator(schema, registry=METASCHEMAS)
    try:
        errors = sorted(validator.iter_errors(parsed), key=lambda e: [str(p) for p in e.absolute_path])
    except Exception as exc:  # unresolvable $ref and the like
        return [{"path": "", "message": f"invalid {label}: {exc}"}]
    return [{"path": _issue_path(e), "message": e.message} for e in errors]


def validate_call_output(schema: Dict[str, Any], text: str) -> Tuple[Optional[Any], List[Dict[str, str]]]:
    """Check a final model reply against the call's `dak:output_schema`.
    Returns `(parsed_json, [])` on success, `(None, issues)` otherwise, each
    issue being `{"path": "a/b", "message": "..."}`."""
    issues = _schema_error(schema, "output_schema")
    if issues:
        return None, issues
    try:
        parsed = json.loads(text, parse_constant=_reject_constant)
    except (ValueError, RecursionError) as exc:  # RecursionError: absurdly deep nesting
        return None, [{"path": "", "message": f"invalid JSON: {exc}"}]
    issues = _json_schema_errors(schema, parsed, "output_schema")
    return (None, issues) if issues else (parsed, [])


def validate_call_inspection(call_settings: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """An error dict when `dak:inspection` is present but malformed, or names
    an endpoint the operator does not allow (the call must then not reach any
    LLM)."""
    spec = call_settings.get(STATE_CALL_INSPECTION)
    if spec is None:
        return None

    def endpoint_ok(endpoint, *required):
        timeout = endpoint.get("timeout_seconds", INSPECTION_TIMEOUT_S) if isinstance(endpoint, Mapping) else None
        return (isinstance(endpoint, Mapping) and all(isinstance(endpoint.get(k), str) for k in ("url", *required))
                and isinstance(timeout, (int, float)) and not isinstance(timeout, bool)
                and 0 < timeout <= MAX_INSPECTION_TIMEOUT_S)

    endpoints = {k: spec[k] for k in ("http", "mcp") if k in spec} if isinstance(spec, Mapping) else {}
    schema = spec.get("json_schema", {}) if isinstance(spec, Mapping) else None
    shape_ok = (
        isinstance(spec, Mapping) and spec and set(spec) <= set(INSPECTION_KINDS)
        and isinstance(schema, Mapping) and not _schema_error(schema, "inspection json_schema")
        and ("http" not in endpoints or endpoint_ok(endpoints["http"]))
        and ("mcp" not in endpoints or endpoint_ok(endpoints["mcp"], "tool"))
    )
    if not shape_ok:
        return {"error": "invalid_inspection",
                "expected": '{"json_schema"?: {<a valid JSON Schema>}, '
                            '"http"?: {"url": "...", "timeout_seconds"?: 10 (at most 60)}, '
                            '"mcp"?: {"url": "...", "tool": "...", "timeout_seconds"?: 10 (at most 60)}}'}
    urls = [e["url"].strip() for e in endpoints.values()]
    raw = os.environ.get(ALLOWED_INSPECTION_URLS_ENV)
    allowed = frozenset(u.strip() for u in (raw or "").split(",") if u.strip())
    refused = [u for u in urls if u not in allowed]
    if refused:
        return {"error": "inspection_url_not_allowed", "requested_urls": refused, "allowed_urls": sorted(allowed)}
    return None


def _unavailable(message: str) -> Dict[str, Any]:
    """An issue saying the check itself could not run (endpoint down, timed
    out, unreadable answer): no regenerated reply can fix it."""
    return {"path": "", "message": message, "unavailable": True}


async def _http_inspection_errors(spec: Mapping[str, Any], parsed: Any) -> List[Dict[str, Any]]:
    """POST the parsed reply to the caller's endpoint; its errors, or one
    describing why the endpoint could not answer."""
    try:
        async with httpx.AsyncClient(timeout=float(spec.get("timeout_seconds", INSPECTION_TIMEOUT_S))) as client:
            response = await client.post(spec["url"].strip(), json=parsed)
        response.raise_for_status()
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        return [_unavailable(f"inspection endpoint error: {exc}")]
    try:
        payload = response.json()
    except ValueError:
        return [_unavailable(f"inspection endpoint returned non-JSON: {response.text[:200]}")]
    return _verdict_errors(payload, "inspection endpoint")


def _verdict_errors(payload: Any, source: str) -> List[Dict[str, Any]]:
    """The errors in a caller's {"valid": bool, "errors": [...]} answer."""
    if isinstance(payload, Mapping) and payload.get("valid") is True:
        return []
    errors = payload.get("errors") if isinstance(payload, Mapping) else None
    if isinstance(errors, list) and errors:
        # `unavailable` is DAK's own mark (`_unavailable`): not the caller's to set.
        return [{k: v for k, v in e.items() if k != "unavailable"}
                if isinstance(e, Mapping) and isinstance(e.get("message"), str)
                else {"path": str(e.get("path", "")) if isinstance(e, Mapping) else "",
                      "message": (json.dumps(e, ensure_ascii=False) if isinstance(e, Mapping) else str(e))[:200]}
                for e in errors]
    return [{"path": "", "message": f"{source} reported invalid, no errors given"}]


async def _mcp_inspection_errors(spec: Mapping[str, Any], parsed: Any) -> List[Dict[str, Any]]:
    """Call the caller's MCP inspection tool once, directly (not as a tool the
    model sees); its errors, or one describing why it could not answer."""
    # Imported here: calls without an MCP inspection do not load the client.
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    timeout = float(spec.get("timeout_seconds", INSPECTION_TIMEOUT_S))
    try:
        # One deadline for the whole exchange. No closing DELETE of the MCP
        # session: it would run after the deadline has fired, unbounded by it.
        async with asyncio.timeout(timeout):
            # No redirects: an allowed URL must not lead the agent to another host.
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as http_client, \
                    streamable_http_client(spec["url"].strip(), http_client=http_client,
                                           terminate_on_close=False) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(spec["tool"], arguments={"data": parsed})
    except TimeoutError:
        return [_unavailable(f"inspection MCP call timed out after {timeout:g} s")]
    except Exception as exc:  # the caller's server, the network, or the mcp client
        while isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1:  # anyio task groups
            exc = exc.exceptions[0]
        return [_unavailable(f"inspection MCP call failed: {type(exc).__name__}: {exc}"[:200])]
    text = "".join(getattr(c, "text", "") or "" for c in (result.content or []))
    if getattr(result, "isError", False):
        return [_unavailable(f"inspection MCP tool failed: {text[:200]}")]
    if not text:
        return [_unavailable("inspection MCP returned no content")]
    try:
        payload = json.loads(text)
    except ValueError:
        return [_unavailable(f"inspection MCP returned non-JSON: {text[:200]}")]
    return _verdict_errors(payload, "inspection MCP")


async def run_inspection(spec: Mapping[str, Any], parsed: Any) -> List[Dict[str, Any]]:
    """Run every check `dak:inspection` names on a parsed reply; all their
    errors ([] when it passes). Refuses (without calling anything) a spec
    `validate_call_inspection` would refuse."""
    refused = validate_call_inspection({STATE_CALL_INSPECTION: spec})
    if refused:
        return [_unavailable(json.dumps(refused))]
    errors: List[Dict[str, Any]] = []
    if "json_schema" in spec:
        errors += _json_schema_errors(spec["json_schema"], parsed, "inspection json_schema")
    if "http" in spec:
        errors += await _http_inspection_errors(spec["http"], parsed)
    if "mcp" in spec:
        errors += await _mcp_inspection_errors(spec["mcp"], parsed)
    return errors


def call_tool_names(value: Any) -> Optional[List[str]]:
    """The names in `dak:tools` (list form, or the dict's "names"); None when
    the dict form gives none."""
    if isinstance(value, list):
        return value
    if isinstance(value, Mapping):
        return value.get("names")
    return None


def call_offers_transfer(call_settings: Dict[str, Any]) -> bool:
    """Whether this call keeps the A2A peers: `dak:tools` is not given, or it
    names transfer_to_agent (ADK adds that tool from sub_agents on its own)."""
    call_tools = call_settings.get(STATE_CALL_TOOLS)
    return call_tools is None or TRANSFER_TOOL in (call_tool_names(call_tools) or [])


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
