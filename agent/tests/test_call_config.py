from types import SimpleNamespace

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
