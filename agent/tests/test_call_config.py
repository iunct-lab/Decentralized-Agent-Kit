import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import mcp
import mcp.client.streamable_http
import pytest
from google.adk.sessions.state import State

from dak_agent import call_config


def _context(state=None, custom_metadata=None):
    run_config = SimpleNamespace(custom_metadata=custom_metadata)
    return SimpleNamespace(
        state=State(value=dict(state or {}), delta={}),
        _invocation_context=SimpleNamespace(run_config=run_config),
    )


def test_resolve_dak_settings_prefers_state_over_custom_metadata():
    ctx = _context(
        state={"dak:instruction": "from state", "other": "ignored"},
        custom_metadata={
            "dak:instruction": "from custom_metadata",
            "a2a_metadata": {"dak:instruction": "from a2a"},
        },
    )

    assert call_config.resolve_dak_settings(ctx) == {"dak:instruction": "from state"}


def test_resolve_dak_settings_reads_a2a_metadata():
    ctx = _context(custom_metadata={"a2a_metadata": {"dak:instruction": "from a2a", "trace": "x"}})

    assert call_config.resolve_dak_settings(ctx) == {"dak:instruction": "from a2a"}


def test_resolve_dak_settings_reads_output_schema():
    schema = {"type": "object", "properties": {"date": {"type": "string"}}, "required": ["date"]}
    ctx = _context(state={call_config.STATE_CALL_OUTPUT_SCHEMA: schema})

    assert call_config.resolve_dak_settings(ctx)["dak:output_schema"] == schema


def test_resolve_dak_settings_without_run_config_reads_state_only():
    ctx = SimpleNamespace(state=State(value={"dak:instruction": "s"}, delta={}))

    assert call_config.resolve_dak_settings(ctx) == {"dak:instruction": "s"}


DATE_SCHEMA = {"type": "object", "properties": {"date": {"type": "string"}}, "required": ["date"]}


def test_validate_call_output_returns_field_path_and_reason():
    parsed, issues = call_config.validate_call_output(DATE_SCHEMA, '{"note": "missing date"}')

    assert parsed is None
    assert len(issues) == 1
    assert issues[0]["path"] == "date"
    assert "required" in issues[0]["message"]


def test_validate_call_output_reports_nested_path():
    schema = {"type": "object", "properties": {"summary": {"type": "object", "required": ["title"], "properties": {
        "count": {"type": "integer"}, "title": {"type": "string"}}}}}

    _, issues = call_config.validate_call_output(schema, '{"summary": {"count": "three"}}')

    assert sorted(i["path"] for i in issues) == ["summary/count", "summary/title"]


def test_validate_call_output_accepts_matching_json():
    assert call_config.validate_call_output(DATE_SCHEMA, '{"date": "2026-09-22"}') == ({"date": "2026-09-22"}, [])


def test_validate_call_output_reports_invalid_json():
    parsed, issues = call_config.validate_call_output(DATE_SCHEMA, "not json")

    assert parsed is None
    assert issues[0]["path"] == ""
    assert issues[0]["message"].startswith("invalid JSON:")


def test_validate_call_output_reports_invalid_schema():
    parsed, issues = call_config.validate_call_output({"type": "no-such-type"}, "{}")

    assert parsed is None
    assert issues[0]["message"].startswith("invalid output_schema:")


def test_validate_call_output_resolves_local_refs():
    schema = {"$defs": {"d": {"type": "string"}}, "type": "object",
              "properties": {"date": {"$ref": "#/$defs/d"}}}

    assert call_config.validate_call_output(schema, '{"date": "x"}') == ({"date": "x"}, [])
    assert call_config.validate_call_output(schema, '{"date": 1}')[1][0]["path"] == "date"


def test_validate_call_output_reports_unresolvable_ref():
    parsed, issues = call_config.validate_call_output({"$ref": "#/$defs/missing"}, "{}")

    assert parsed is None
    assert issues[0]["message"].startswith("invalid output_schema:")


def test_validate_call_output_never_fetches_remote_refs():
    """A caller-supplied schema must not make the agent fetch URLs (SSRF)."""
    from unittest.mock import patch

    with patch("urllib.request.urlopen", side_effect=AssertionError("fetched a remote $ref")) as urlopen:
        parsed, issues = call_config.validate_call_output(
            {"$ref": "http://169.254.169.254/latest/meta-data/"}, "{}")

    urlopen.assert_not_called()
    assert parsed is None
    assert issues[0]["message"].startswith("invalid output_schema:")


def test_validate_call_output_rejects_non_standard_json_constants():
    for text in ("NaN", "Infinity", "-Infinity", '{"x": NaN}'):
        parsed, issues = call_config.validate_call_output({}, text)
        assert parsed is None, text
        assert issues[0]["message"].startswith("invalid JSON:"), text


def test_validate_call_output_reports_too_deeply_nested_json():
    parsed, issues = call_config.validate_call_output({}, "[" * 100_000)

    assert parsed is None
    assert issues[0]["message"].startswith("invalid JSON:")


def test_resolve_model_selection_returns_default_when_unspecified(monkeypatch):
    monkeypatch.setenv("DAK_ALLOWED_MODELS", "a,b")

    assert call_config.resolve_model_selection({}, "default-model") == ("default-model", None)


def test_resolve_model_selection_rejects_when_allow_list_unset(monkeypatch):
    monkeypatch.delenv("DAK_ALLOWED_MODELS", raising=False)

    model, error = call_config.resolve_model_selection({"dak:model": "a"}, "default-model")

    assert model == "default-model"
    assert error == {"error": "model_not_allowed", "requested_model": "a", "allowed_models": []}


def test_resolve_model_selection_rejects_when_not_in_allow_list(monkeypatch):
    monkeypatch.setenv("DAK_ALLOWED_MODELS", " b , a ,,")

    model, error = call_config.resolve_model_selection({"dak:model": "c"}, "default-model")

    assert model == "default-model"
    assert error["error"] == "model_not_allowed"
    assert error["requested_model"] == "c"
    assert error["allowed_models"] == ["a", "b"]


def test_resolve_model_selection_accepts_when_allowed(monkeypatch):
    monkeypatch.setenv("DAK_ALLOWED_MODELS", "a,b")

    assert call_config.resolve_model_selection({"dak:model": "a"}, "default-model") == ("a", None)


def test_resolve_model_selection_rejects_non_string_model(monkeypatch):
    monkeypatch.setenv("DAK_ALLOWED_MODELS", "a")

    model, error = call_config.resolve_model_selection({"dak:model": ["a"]}, "default-model")

    assert model == "default-model"
    assert error["error"] == "model_not_allowed"


def test_caller_mcp_servers_are_refused_when_allow_list_unset(monkeypatch):
    monkeypatch.delenv("DAK_ALLOWED_MCP_URLS", raising=False)

    servers, error = call_config.resolve_caller_mcp_servers(
        {"dak:tools": {"mcp_servers": [{"url": "http://caller:9000/mcp"}]}})

    assert servers is None
    assert error == {"error": "mcp_server_not_allowed", "requested_urls": ["http://caller:9000/mcp"],
                     "allowed_urls": []}


def test_caller_mcp_servers_are_accepted_when_allowed(monkeypatch):
    monkeypatch.setenv("DAK_ALLOWED_MCP_URLS", "http://caller:9000/mcp, http://other/mcp")

    servers, error = call_config.resolve_caller_mcp_servers(
        {"dak:tools": {"mcp_servers": [{"url": "http://caller:9000/mcp", "type": "sse"}, {"url": "http://other/mcp"}]}})

    assert error is None
    assert servers == [{"url": "http://caller:9000/mcp", "type": "sse"}, {"url": "http://other/mcp", "type": "http"}]


def test_caller_mcp_servers_refuse_the_whole_call_if_any_url_is_not_allowed(monkeypatch):
    monkeypatch.setenv("DAK_ALLOWED_MCP_URLS", "http://caller:9000/mcp")

    servers, error = call_config.resolve_caller_mcp_servers(
        {"dak:tools": {"mcp_servers": [{"url": "http://caller:9000/mcp"}, {"url": "http://169.254.169.254/"}]}})

    assert servers is None
    assert error["requested_urls"] == ["http://169.254.169.254/"]


def test_malformed_caller_mcp_servers_are_refused(monkeypatch):
    monkeypatch.setenv("DAK_ALLOWED_MCP_URLS", "http://caller:9000/mcp")
    for bad in ({"mcp_servers": "http://caller:9000/mcp"}, {"mcp_servers": [{"url": "http://caller:9000/mcp", "type": "ws"}]},
                {"mcp_servers": [{"nourl": 1}]}):
        servers, error = call_config.resolve_caller_mcp_servers({"dak:tools": bad})
        assert servers is None and error["error"] == "invalid_mcp_servers", bad


def test_no_caller_mcp_servers_is_neither_servers_nor_error():
    assert call_config.resolve_caller_mcp_servers({}) == (None, None)
    assert call_config.resolve_caller_mcp_servers({"dak:tools": ["a"]}) == (None, None)
    assert call_config.resolve_caller_mcp_servers({"dak:tools": {"names": ["a"]}}) == (None, None)


def _no_operator_limits(monkeypatch):
    for name in (call_config.MAX_LLM_CALLS_ENV, call_config.MAX_SECONDS_ENV, call_config.MAX_OUTPUT_TOKENS_ENV):
        monkeypatch.delenv(name, raising=False)


def test_resolve_call_limits_defaults_to_unbounded(monkeypatch):
    _no_operator_limits(monkeypatch)

    assert call_config.resolve_call_limits({}) == call_config.CallLimits(0, 0.0, 0)


def test_resolve_call_limits_caller_narrows_operator_default(monkeypatch):
    _no_operator_limits(monkeypatch)
    monkeypatch.setenv("DAK_MAX_LLM_CALLS", "10")
    monkeypatch.setenv("DAK_MAX_TURN_SECONDS", "60")
    monkeypatch.setenv("DAK_MAX_OUTPUT_TOKENS", "4096")

    limits = call_config.resolve_call_limits(
        {"dak:max_llm_calls": 3, "dak:max_seconds": 1.5, "dak:max_output_tokens": 256})

    assert limits == call_config.CallLimits(max_llm_calls=3, max_seconds=1.5, max_output_tokens=256)


def test_resolve_call_limits_caller_cannot_exceed_operator_default(monkeypatch):
    _no_operator_limits(monkeypatch)
    monkeypatch.setenv("DAK_MAX_LLM_CALLS", "5")
    monkeypatch.setenv("DAK_MAX_TURN_SECONDS", "30")
    monkeypatch.setenv("DAK_MAX_OUTPUT_TOKENS", "512")

    limits = call_config.resolve_call_limits(
        {"dak:max_llm_calls": 100, "dak:max_seconds": 900, "dak:max_output_tokens": 100000})

    assert limits == call_config.CallLimits(max_llm_calls=5, max_seconds=30.0, max_output_tokens=512)


def test_resolve_call_limits_caller_zero_keeps_operator_default(monkeypatch):
    """0 means "no limit of my own", not "lift the operator's limit"."""
    _no_operator_limits(monkeypatch)
    monkeypatch.setenv("DAK_MAX_LLM_CALLS", "5")

    assert call_config.resolve_call_limits({"dak:max_llm_calls": 0}).max_llm_calls == 5


def test_resolve_call_limits_no_operator_default_uses_caller_value(monkeypatch):
    _no_operator_limits(monkeypatch)

    limits = call_config.resolve_call_limits(
        {"dak:max_llm_calls": 7, "dak:max_seconds": 2, "dak:max_output_tokens": 128})

    assert limits == call_config.CallLimits(max_llm_calls=7, max_seconds=2.0, max_output_tokens=128)


def test_resolve_call_limits_ignores_invalid_values(monkeypatch):
    _no_operator_limits(monkeypatch)
    monkeypatch.setenv("DAK_MAX_LLM_CALLS", "many")
    monkeypatch.setenv("DAK_MAX_TURN_SECONDS", "4")

    limits = call_config.resolve_call_limits(
        {"dak:max_llm_calls": 3, "dak:max_seconds": "soon", "dak:max_output_tokens": -5})

    # A broken operator value is unset (the caller's 3 applies); a broken
    # caller value is ignored (the operator's 4 s applies).
    assert limits == call_config.CallLimits(max_llm_calls=3, max_seconds=4.0, max_output_tokens=0)


def test_resolve_call_limits_ignores_overflowing_values(monkeypatch):
    """JSON `1e309` arrives as float("inf"); int() of it raises OverflowError."""
    _no_operator_limits(monkeypatch)
    monkeypatch.setenv("DAK_MAX_LLM_CALLS", "5")

    limits = call_config.resolve_call_limits(
        {"dak:max_llm_calls": float("inf"), "dak:max_output_tokens": float("inf")})

    assert limits == call_config.CallLimits(max_llm_calls=5, max_seconds=0.0, max_output_tokens=0)


INSPECT_URL = "http://caller:9000/validate"


def _http_reply(payload, status=200):
    return httpx.Response(status, json=payload, request=httpx.Request("POST", INSPECT_URL))


def test_validate_call_inspection_refuses_urls_when_allow_list_unset(monkeypatch):
    monkeypatch.delenv("DAK_ALLOWED_INSPECTION_URLS", raising=False)

    error = call_config.validate_call_inspection({"dak:inspection": {"http": {"url": INSPECT_URL}}})

    assert error == {"error": "inspection_url_not_allowed", "requested_urls": [INSPECT_URL], "allowed_urls": []}


def test_validate_call_inspection_accepts_allowed_url_and_schema_only(monkeypatch):
    monkeypatch.setenv("DAK_ALLOWED_INSPECTION_URLS", f"{INSPECT_URL}, http://other/validate")

    assert call_config.validate_call_inspection({"dak:inspection": {"http": {"url": INSPECT_URL}}}) is None
    assert call_config.validate_call_inspection({"dak:inspection": {"json_schema": DATE_SCHEMA}}) is None
    assert call_config.validate_call_inspection({}) is None


def test_validate_call_inspection_refuses_malformed_specs(monkeypatch):
    monkeypatch.setenv("DAK_ALLOWED_INSPECTION_URLS", INSPECT_URL)
    for bad in ({}, "http://x", {"json_schema": "x"}, {"http": INSPECT_URL}, {"http": {"url": INSPECT_URL, "timeout_seconds": 0}},
                {"shell": "rm"}, {"http": None}, {"json_schema": DATE_SCHEMA, "http": None},
                {"http": {"url": INSPECT_URL, "timeout_seconds": float("inf")}},
                {"http": {"url": INSPECT_URL, "timeout_seconds": call_config.MAX_INSPECTION_TIMEOUT_S + 1}},
                {"json_schema": {"type": 5}}):
        error = call_config.validate_call_inspection({"dak:inspection": bad})
        assert error["error"] == "invalid_inspection", bad


@pytest.mark.asyncio
async def test_run_inspection_json_schema_reports_errors():
    errors = await call_config.run_inspection({"json_schema": DATE_SCHEMA}, {"note": "missing date"})

    assert errors == [{"path": "date", "message": "'date' is a required property"}]
    assert await call_config.run_inspection({"json_schema": DATE_SCHEMA}, {"date": "2026-09-22"}) == []


@pytest.mark.asyncio
async def test_run_inspection_http_combines_with_json_schema(monkeypatch):
    monkeypatch.setenv("DAK_ALLOWED_INSPECTION_URLS", INSPECT_URL)
    caller_errors = [{"path": "date", "message": "not a business day"}]
    post = AsyncMock(return_value=_http_reply({"valid": False, "errors": caller_errors}))
    monkeypatch.setattr(httpx.AsyncClient, "post", post)

    errors = await call_config.run_inspection(
        {"json_schema": {"type": "object", "required": ["tz"]}, "http": {"url": INSPECT_URL}}, {"date": "2026-09-26"})

    assert errors == [{"path": "tz", "message": "'tz' is a required property"}] + caller_errors
    post.assert_awaited_once()
    assert post.await_args.args[-1] == INSPECT_URL and post.await_args.kwargs["json"] == {"date": "2026-09-26"}


@pytest.mark.asyncio
async def test_run_inspection_http_accepts_valid_and_reports_bad_replies(monkeypatch):
    monkeypatch.setenv("DAK_ALLOWED_INSPECTION_URLS", INSPECT_URL)
    spec = {"http": {"url": INSPECT_URL}}
    for reply, expected in ((_http_reply({"valid": True}), None),
                            (_http_reply({"valid": False}), "reported invalid, no errors given"),
                            (_http_reply({"detail": "boom"}, status=500), "inspection endpoint error"),
                            (httpx.Response(200, text="ok", request=httpx.Request("POST", INSPECT_URL)), "non-JSON")):
        monkeypatch.setattr(httpx.AsyncClient, "post", AsyncMock(return_value=reply))
        errors = await call_config.run_inspection(spec, {"date": "2026-09-22"})
        if expected is None:
            assert errors == []
        else:
            assert len(errors) == 1 and expected in errors[0]["message"], errors


@pytest.mark.asyncio
async def test_run_inspection_http_turns_odd_errors_into_issues(monkeypatch):
    monkeypatch.setenv("DAK_ALLOWED_INSPECTION_URLS", INSPECT_URL)
    monkeypatch.setattr(httpx.AsyncClient, "post", AsyncMock(return_value=_http_reply({"valid": False, "errors": ["bad date", 3]})))

    errors = await call_config.run_inspection({"http": {"url": INSPECT_URL}}, {})

    assert errors == [{"path": "", "message": "bad date"}, {"path": "", "message": "3"}]


@pytest.mark.asyncio
async def test_run_inspection_http_fills_in_errors_without_a_message(monkeypatch):
    monkeypatch.setenv("DAK_ALLOWED_INSPECTION_URLS", INSPECT_URL)
    reply = _http_reply({"valid": False, "errors": [{"path": "date", "msg": "日付が不正"}, {"m": "x" * 500}]})
    monkeypatch.setattr(httpx.AsyncClient, "post", AsyncMock(return_value=reply))

    errors = await call_config.run_inspection({"http": {"url": INSPECT_URL}}, {})

    assert errors[0] == {"path": "date", "message": '{"path": "date", "msg": "日付が不正"}'}
    assert len(errors[1]["message"]) == 200


@pytest.mark.asyncio
async def test_run_inspection_http_cannot_mark_its_own_errors_unavailable(monkeypatch):
    """`unavailable` is DAK's: a caller's verdict cannot turn off regeneration."""
    monkeypatch.setenv("DAK_ALLOWED_INSPECTION_URLS", INSPECT_URL)
    reply = _http_reply({"valid": False, "errors": [{"path": "date", "message": "bad", "unavailable": True}]})
    monkeypatch.setattr(httpx.AsyncClient, "post", AsyncMock(return_value=reply))

    assert await call_config.run_inspection({"http": {"url": INSPECT_URL}}, {}) == [{"path": "date", "message": "bad"}]


@pytest.mark.asyncio
async def test_run_inspection_http_reports_invalid_url(monkeypatch):
    monkeypatch.setenv("DAK_ALLOWED_INSPECTION_URLS", "http://[::1/v")

    errors = await call_config.run_inspection({"http": {"url": "http://[::1/v"}}, {})

    assert len(errors) == 1 and "inspection endpoint error" in errors[0]["message"]


@pytest.mark.asyncio
async def test_run_inspection_http_reports_connection_failure(monkeypatch):
    monkeypatch.setenv("DAK_ALLOWED_INSPECTION_URLS", INSPECT_URL)
    monkeypatch.setattr(httpx.AsyncClient, "post", AsyncMock(side_effect=httpx.ConnectError("refused")))

    errors = await call_config.run_inspection({"http": {"url": INSPECT_URL}}, {"date": "2026-09-22"})

    # The check could not run: no regenerated reply would fix that (#245).
    assert len(errors) == 1 and "inspection endpoint error" in errors[0]["message"] and errors[0]["unavailable"]


@pytest.mark.asyncio
async def test_run_inspection_never_calls_a_url_outside_the_allow_list(monkeypatch):
    monkeypatch.delenv("DAK_ALLOWED_INSPECTION_URLS", raising=False)
    post = AsyncMock(return_value=_http_reply({"valid": True}))
    monkeypatch.setattr(httpx.AsyncClient, "post", post)

    errors = await call_config.run_inspection({"http": {"url": "http://169.254.169.254/"}}, {})

    assert len(errors) == 1 and "inspection_url_not_allowed" in errors[0]["message"]
    post.assert_not_awaited()


MCP_URL = "http://caller:9000/mcp"


def _fake_mcp(monkeypatch, *, text=None, connect_error=None, is_error=False, delay=0.0):
    """Stand-ins for the mcp client: the session's call_tool answers `text`."""
    calls = []

    @asynccontextmanager
    async def client(url, **kwargs):
        if connect_error:
            raise connect_error
        calls.append(("connect", url, kwargs["http_client"].follow_redirects, kwargs["terminate_on_close"]))
        yield "read", "write", lambda: None

    class Session:
        def __init__(self, read, write):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def initialize(self):
            calls.append(("initialize",))

        async def call_tool(self, name, arguments):
            calls.append(("call_tool", name, arguments))
            await asyncio.sleep(delay)
            return SimpleNamespace(content=[SimpleNamespace(text=text)] if text is not None else [], isError=is_error)

    monkeypatch.setattr(mcp.client.streamable_http, "streamable_http_client", client)
    monkeypatch.setattr(mcp, "ClientSession", Session)
    return calls


@pytest.mark.asyncio
async def test_run_inspection_mcp_reports_errors(monkeypatch):
    monkeypatch.setenv("DAK_ALLOWED_INSPECTION_URLS", MCP_URL)
    calls = _fake_mcp(monkeypatch, text='{"valid": false, "errors": [{"path": "date", "message": "missing"}]}')

    errors = await call_config.run_inspection({"mcp": {"url": MCP_URL, "tool": "check_plan"}}, {"note": "x"})

    assert errors == [{"path": "date", "message": "missing"}]
    # No redirects: an allowed URL must not lead the agent to another host.
    # No closing DELETE: it would run after the deadline has fired.
    assert calls == [("connect", MCP_URL, False, False), ("initialize",), ("call_tool", "check_plan", {"data": {"note": "x"}})]


@pytest.mark.asyncio
async def test_run_inspection_mcp_accepts_valid_and_reports_bad_replies(monkeypatch):
    monkeypatch.setenv("DAK_ALLOWED_INSPECTION_URLS", MCP_URL)
    spec = {"mcp": {"url": MCP_URL, "tool": "check_plan"}}
    for text, expected in (('{"valid": true}', None), ('{"valid": false}', "reported invalid, no errors given"),
                           (None, "returned no content"), ("fine", "returned non-JSON")):
        _fake_mcp(monkeypatch, text=text)
        errors = await call_config.run_inspection(spec, {})
        if expected is None:
            assert errors == []
        else:
            assert len(errors) == 1 and expected in errors[0]["message"], errors


@pytest.mark.asyncio
async def test_run_inspection_mcp_reports_connection_failure(monkeypatch):
    monkeypatch.setenv("DAK_ALLOWED_INSPECTION_URLS", MCP_URL)
    _fake_mcp(monkeypatch, connect_error=httpx.ConnectError("refused"))

    errors = await call_config.run_inspection({"mcp": {"url": MCP_URL, "tool": "check_plan"}}, {})

    assert len(errors) == 1 and "inspection MCP call failed" in errors[0]["message"]


@pytest.mark.asyncio
async def test_run_inspection_mcp_bounds_the_whole_call_by_its_timeout(monkeypatch):
    monkeypatch.setenv("DAK_ALLOWED_INSPECTION_URLS", MCP_URL)
    _fake_mcp(monkeypatch, text='{"valid": true}', delay=5)

    started = asyncio.get_running_loop().time()
    errors = await call_config.run_inspection({"mcp": {"url": MCP_URL, "tool": "t", "timeout_seconds": 0.05}}, {})

    assert asyncio.get_running_loop().time() - started < 1
    assert errors == [{"path": "", "message": "inspection MCP call timed out after 0.05 s", "unavailable": True}]


@pytest.mark.asyncio
async def test_run_inspection_mcp_keeps_failure_messages_short(monkeypatch):
    monkeypatch.setenv("DAK_ALLOWED_INSPECTION_URLS", MCP_URL)
    # The real client raises transport errors from an anyio task group.
    _fake_mcp(monkeypatch, connect_error=ExceptionGroup("unhandled errors in a TaskGroup", [httpx.ConnectError("x" * 1000)]))

    errors = await call_config.run_inspection({"mcp": {"url": MCP_URL, "tool": "t"}}, {})

    assert errors[0]["message"].startswith("inspection MCP call failed: ConnectError")
    assert len(errors[0]["message"]) == 200


@pytest.mark.asyncio
async def test_run_inspection_mcp_fills_in_errors_without_a_message(monkeypatch):
    monkeypatch.setenv("DAK_ALLOWED_INSPECTION_URLS", MCP_URL)
    _fake_mcp(monkeypatch, text='{"valid": false, "errors": [{"msg": "bad date"}]}')

    errors = await call_config.run_inspection({"mcp": {"url": MCP_URL, "tool": "t"}}, {})

    assert errors == [{"path": "", "message": '{"msg": "bad date"}'}]


@pytest.mark.asyncio
async def test_run_inspection_mcp_reports_tool_error(monkeypatch):
    monkeypatch.setenv("DAK_ALLOWED_INSPECTION_URLS", MCP_URL)
    _fake_mcp(monkeypatch, text="Unknown tool: check_plan", is_error=True)

    errors = await call_config.run_inspection({"mcp": {"url": MCP_URL, "tool": "check_plan"}}, {})

    assert errors == [{"path": "", "message": "inspection MCP tool failed: Unknown tool: check_plan", "unavailable": True}]


def test_validate_call_inspection_checks_mcp_endpoints(monkeypatch):
    monkeypatch.setenv("DAK_ALLOWED_INSPECTION_URLS", MCP_URL)

    assert call_config.validate_call_inspection({"dak:inspection": {"mcp": {"url": MCP_URL, "tool": "t"}}}) is None
    assert call_config.validate_call_inspection(
        {"dak:inspection": {"mcp": {"url": MCP_URL}}})["error"] == "invalid_inspection"
    error = call_config.validate_call_inspection(
        {"dak:inspection": {"mcp": {"url": "http://internal/mcp", "tool": "t"}, "http": {"url": MCP_URL}}})
    assert error["error"] == "inspection_url_not_allowed" and error["requested_urls"] == ["http://internal/mcp"]
