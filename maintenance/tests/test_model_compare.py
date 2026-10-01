"""compare-models: the same fixed inputs through the four maintenance prompts, per model, as one table."""
import json

import pytest

from dak_maintenance import llm_client, model_compare
from dak_maintenance.cli import main
from dak_maintenance.model_compare import CaseResult, ModelSpec, load_inputs, load_models, render_table, run_case


@pytest.fixture
def inputs():
    return load_inputs()


def answering(case_replies):
    """A fake complete() that answers by what the prompt asks for."""
    def complete(prompt: str) -> str:
        if "web search queries" in prompt:
            return '["agent protocol"]'
        if "You evaluate candidate technologies" in prompt:
            return case_replies.get("watch", "[]")
        if "quarterly review" in prompt:
            return case_replies.get("charter-review", "{}")
        if "risk assessor" in prompt:
            return case_replies.get("triage", "{}")
        return case_replies.get("feature-sync", "[]")
    return complete


WATCH = '[{"title": "[tech-watch] MCP の採用検討", "subject": "MCP", "url": "u", "fit": "f", "sketch": "s"}]'
FEATURE = '[{"title": "[feature-sync] x", "feature": "f", "component": "agent", "sketch": "s"}]'
CHARTER = '{"title": "Charter review", "landscape": "l", "revisions": "r", "domains": "d"}'
TRIAGE = '{"level": "breaking", "summary": "proxies removed"}'


@pytest.mark.parametrize("case,reply,count", [
    ("watch", WATCH, 1), ("feature-sync", FEATURE, 1), ("charter-review", CHARTER, 1), ("triage", TRIAGE, 1),
])
def test_run_case_reads_json_and_counts(inputs, case, reply, count):
    r = run_case(case, answering({case: reply}), inputs)
    assert (r.parsed_ok, r.count, r.error) == (True, count, "")
    assert r.latency_sec >= 0
    assert r.titles


def test_triage_reports_the_level_it_read(inputs):
    r = run_case("triage", answering({"triage": TRIAGE}), inputs)
    assert r.titles == ["breaking: proxies removed"]


@pytest.mark.parametrize("case", ["watch", "feature-sync", "charter-review", "triage"])
def test_broken_json_is_not_parsed_ok(inputs, case):
    r = run_case(case, lambda prompt: "sorry, I cannot", inputs)
    assert not r.parsed_ok
    assert r.count == 0


@pytest.mark.parametrize("case", ["watch", "feature-sync", "charter-review", "triage"])
def test_an_api_error_is_recorded_and_does_not_raise(inputs, case):
    def fail(prompt):
        raise RuntimeError("400 Bad Request: temperature is not supported")
    r = run_case(case, fail, inputs)
    assert not r.parsed_ok
    assert "temperature" in r.error


def test_render_table_prices_tokens_and_marks_unpriced_models():
    results = [
        CaseResult("cheap", "watch", True, 1, 1.5, 1_000_000, 100_000, "", ["[tech-watch] A"]),
        CaseResult("free", "watch", False, 0, 0.2, None, None, "boom", []),
    ]
    table = render_table(results, {"cheap": {"input": 0.1, "output": 0.5}})
    assert "| cheap | watch | yes | 1 | 1.5 | 1000000 | 100000 | $0.1500 |" in table
    assert "| free | watch | no | 0 | 0.2 | — | — | 価格未指定 |" in table
    assert "boom" in table
    assert "[tech-watch] A" in table


def test_load_models_keeps_only_the_key_variable_name(tmp_path):
    p = tmp_path / "models.json"
    p.write_text(json.dumps([{"name": "luna", "base_url": "https://api.openai.com/v1", "model": "gpt-6-luna",
                              "api_key_env": "OPENAI_API_KEY"}]))
    assert load_models(str(p)) == [ModelSpec("luna", "https://api.openai.com/v1", "gpt-6-luna", "OPENAI_API_KEY")]


class FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def test_make_complete_reports_usage_and_takes_explicit_settings(monkeypatch):
    for key in ("MAINT_LLM_BASE_URL", "MAINT_LLM_MODEL", "MAINT_LLM_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    seen = {}

    def post(url, headers, json, timeout):
        seen.update(url=url, auth=headers["Authorization"], model=json["model"])
        return FakeResponse({"choices": [{"message": {"content": "[]"}}],
                             "usage": {"prompt_tokens": 12, "completion_tokens": 3}})

    monkeypatch.setattr(llm_client.httpx, "post", post)
    usage = []
    complete = llm_client.make_complete(base_url="http://llm/v1", model="m", api_key="k", on_usage=usage.append)
    assert complete("hi") == "[]"
    assert seen == {"url": "http://llm/v1/chat/completions", "auth": "Bearer k", "model": "m"}
    assert usage == [{"prompt_tokens": 12, "completion_tokens": 3}]


def test_cli_compare_models_writes_a_table(tmp_path, monkeypatch):
    models = tmp_path / "models.json"
    models.write_text(json.dumps([{"name": "fake", "base_url": "http://llm/v1", "model": "m", "api_key_env": "FAKE_KEY"}]))
    prices = tmp_path / "prices.json"
    prices.write_text(json.dumps({"fake": {"input": 1.0, "output": 2.0}}))
    monkeypatch.setenv("FAKE_KEY", "k")
    replies = {"watch": WATCH, "feature-sync": FEATURE, "charter-review": CHARTER, "triage": TRIAGE}

    def post(url, headers, json, timeout):
        assert headers["Authorization"] == "Bearer k"
        content = answering(replies)(json["messages"][0]["content"])
        return FakeResponse({"choices": [{"message": {"content": content}}],
                             "usage": {"prompt_tokens": 100, "completion_tokens": 10}})

    monkeypatch.setattr(llm_client.httpx, "post", post)
    out = tmp_path / "compare.md"
    assert main(["compare-models", "--models", str(models), "--prices", str(prices), "--out", str(out)]) == 0
    table = out.read_text()
    for case in model_compare.CASES:
        assert f"| fake | {case} | yes |" in table
    # watch calls the LLM twice (queries + evaluation): its tokens add up
    assert "| fake | watch | yes | 1 |" in table and "| 200 | 20 |" in table


def test_a_model_never_gets_the_maintenance_settings(monkeypatch):
    monkeypatch.setenv("MAINT_LLM_BASE_URL", "http://maintenance/v1")
    monkeypatch.setenv("MAINT_LLM_API_KEY", "maintenance-key")
    seen = []

    def post(url, headers, json, timeout):
        seen.append((url, headers["Authorization"]))
        return FakeResponse({"choices": [{"message": {"content": TRIAGE}}]})

    monkeypatch.setattr(llm_client.httpx, "post", post)
    models = [ModelSpec("keyless", "http://local/v1", "m", ""), ModelSpec("no-url", "", "m", "")]
    results = model_compare.compare(models, ["triage"], load_inputs())
    assert seen == [("http://local/v1/chat/completions", "Bearer not-needed")]
    assert results[1].error and not results[1].parsed_ok


@pytest.mark.parametrize("case,cap", [("watch", 5), ("charter-review", 3)])
def test_every_search_result_reaches_the_model(inputs, case, cap):
    prompts = []

    def complete(prompt):
        prompts.append(prompt)
        return '["q"]' if "web search queries" in prompt else "[]"

    run_case(case, complete, inputs)
    assert len(inputs["search_results"]) > cap
    assert all(r.url in prompts[-1] or r.title in prompts[-1] for r in inputs["search_results"])


def test_an_empty_json_object_is_read_as_json(inputs):
    r = run_case("charter-review", lambda prompt: "{}", inputs)
    assert r.parsed_ok and r.count == 0


def test_broken_json_around_an_empty_object_is_not_parsed_ok(inputs):
    r = run_case("charter-review", lambda prompt: '{"title": invalid, "revisions": {}}', inputs)
    assert not r.parsed_ok


def test_an_empty_object_in_prose_is_read_as_json(inputs):
    r = run_case("charter-review", lambda prompt: "No proposals: {}", inputs)
    assert r.parsed_ok


@pytest.mark.parametrize("prices", ['{"fake": {"in": 0.1, "out": 0.5}}', '{"fake": 0.1}', '[]'])
def test_bad_prices_stop_before_any_model_is_called(tmp_path, monkeypatch, prices):
    models = tmp_path / "models.json"
    models.write_text(json.dumps([{"name": "fake", "base_url": "http://llm/v1", "model": "m", "api_key_env": ""}]))
    p = tmp_path / "prices.json"
    p.write_text(prices)

    def post(*a, **kw):
        raise AssertionError("a model was called")

    monkeypatch.setattr(llm_client.httpx, "post", post)
    assert main(["compare-models", "--models", str(models), "--prices", str(p)]) == 2


def test_an_empty_maintenance_key_is_sent_as_before(monkeypatch):
    monkeypatch.setenv("MAINT_LLM_BASE_URL", "http://llm/v1")
    monkeypatch.setenv("MAINT_LLM_MODEL", "m")
    monkeypatch.setenv("MAINT_LLM_API_KEY", "")
    seen = []
    monkeypatch.setattr(llm_client.httpx, "post", lambda url, headers, json, timeout: seen.append(headers["Authorization"])
                        or FakeResponse({"choices": [{"message": {"content": "[]"}}]}))
    llm_client.make_complete()("hi")
    assert seen == ["Bearer "]


def test_an_http_error_shows_what_the_api_said_on_one_line(inputs):
    import httpx
    req = httpx.Request("POST", "http://llm/v1/chat/completions")
    resp = httpx.Response(400, request=req, json={"error": {"message": "Unsupported parameter: temperature"}})

    def fail(prompt):
        resp.raise_for_status()

    r = run_case("triage", fail, inputs)
    assert "Unsupported parameter: temperature" in r.error
    assert "\n" not in r.error


def test_the_comparison_lists_every_proposal_not_just_two(inputs):
    many = json.dumps([{"title": f"[tech-watch] t{i}", "subject": "s", "url": "u", "fit": "f", "sketch": "s"} for i in range(5)])
    r = run_case("watch", answering({"watch": many}), inputs)
    assert r.count == 5


@pytest.mark.parametrize("args", [["--prices", "missing.json"], ["--prices", "PRICES", "--out", "no-such-dir/compare.md"],
                                  ["--prices", "BOOL"]])
def test_bad_options_stop_before_any_model_is_called(tmp_path, monkeypatch, args):
    models = tmp_path / "models.json"
    models.write_text(json.dumps([{"name": "fake", "base_url": "http://llm/v1", "model": "m", "api_key_env": ""}]))
    (tmp_path / "PRICES").write_text('{"fake": {"input": 0.1, "output": 0.5}}')
    (tmp_path / "BOOL").write_text('{"fake": {"input": true, "output": 0.5}}')
    monkeypatch.chdir(tmp_path)

    def post(*a, **kw):
        raise AssertionError("a model was called")

    monkeypatch.setattr(llm_client.httpx, "post", post)
    assert main(["compare-models", "--models", str(models), *args]) == 2


def test_model_text_stays_on_its_line():
    r = CaseResult("m", "triage", True, 1, 0.1, None, None, "", ["breaking: line one\nline two"])
    assert "- m / triage: breaking: line one line two" in render_table([r], {})
