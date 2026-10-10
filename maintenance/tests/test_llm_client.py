"""make_complete: OpenAI-compatible HTTP (MAINT_LLM_BASE_URL) or Amazon Bedrock
with IAM credentials (MAINT_LLM_MODEL=bedrock/<model or inference profile>)."""
import sys
import types

import pytest
import httpx

from dak_maintenance import llm_client


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in ("MAINT_LLM_BASE_URL", "MAINT_LLM_MODEL", "MAINT_LLM_API_KEY", "AWS_REGION", "AWS_DEFAULT_REGION"):
        monkeypatch.delenv(key, raising=False)


def test_unconfigured_returns_none():
    assert llm_client.make_complete() is None


def test_openai_compatible_needs_a_base_url(monkeypatch):
    monkeypatch.setenv("MAINT_LLM_MODEL", "gemini-3.5-flash")
    assert llm_client.make_complete() is None


class FakeBedrock:
    def __init__(self):
        self.calls = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        return {"stopReason": "end_turn", "output": {"message": {"role": "assistant", "content": [
            {"reasoningContent": {"reasoningText": {"text": "thinking"}}},
            {"text": '[{"title": "x"}]'}]}}}


@pytest.fixture
def fake_boto3(monkeypatch):
    client = FakeBedrock()
    made = {}

    def make_client(service, **kwargs):
        made.update(service=service, **kwargs)
        return client

    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=make_client))
    return client, made


def test_bedrock_model_uses_converse_with_iam_credentials(monkeypatch, fake_boto3):
    client, made = fake_boto3
    monkeypatch.setenv("MAINT_LLM_MODEL", "bedrock/global.openai.gpt-6-luna")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setenv("MAINT_LLM_API_KEY", "must-not-be-used")

    complete = llm_client.make_complete()
    text = complete("hello")

    assert text == '[{"title": "x"}]'  # reasoning blocks are not part of the answer
    assert made["service"] == "bedrock-runtime" and made["region_name"] == "us-west-2"
    call = client.calls[0]
    assert call["modelId"] == "global.openai.gpt-6-luna"
    assert call["messages"] == [{"role": "user", "content": [{"text": "hello"}]}]
    assert "must-not-be-used" not in repr(made) + repr(call)  # IAM (SigV4), not the API key


def test_bedrock_reports_usage(monkeypatch, fake_boto3):
    client, _ = fake_boto3
    reply = client.converse
    client.converse = lambda **kw: {**reply(**kw), "usage": {"inputTokens": 7, "outputTokens": 2, "totalTokens": 9}}
    usage = []
    llm_client.make_complete(model="bedrock/global.openai.gpt-6-luna", on_usage=usage.append)("hi")
    assert usage == [{"prompt_tokens": 7, "completion_tokens": 2}]


def test_bedrock_region_defaults_to_us_east_1(monkeypatch, fake_boto3):
    _, made = fake_boto3
    monkeypatch.setenv("MAINT_LLM_MODEL", "bedrock/global.openai.gpt-6-luna")
    llm_client.make_complete()("hi")
    assert made["region_name"] == "us-east-1"


def test_bedrock_does_not_need_a_base_url(monkeypatch, fake_boto3):
    monkeypatch.setenv("MAINT_LLM_MODEL", "bedrock/global.openai.gpt-6-luna")
    assert llm_client.make_complete() is not None


@pytest.mark.parametrize("stop, content", [
    ("max_tokens", [{"reasoningContent": {"reasoningText": {"text": "long thinking"}}}]),
    ("content_filtered", [{"text": ""}]),
    ("end_turn", [{"reasoningContent": {"reasoningText": {"text": "only thinking"}}}]),
])
def test_bedrock_incomplete_or_empty_answer_raises(monkeypatch, fake_boto3, stop, content):
    """A truncated/filtered/empty reply must not look like "no proposals"."""
    client, _ = fake_boto3
    client.converse = lambda **kw: {"stopReason": stop, "output": {"message": {"content": content}}}
    monkeypatch.setenv("MAINT_LLM_MODEL", "bedrock/global.openai.gpt-6-luna")
    with pytest.raises(RuntimeError):
        llm_client.make_complete()("hi")


def test_bedrock_client_does_not_resend_on_read_timeouts(monkeypatch, fake_boto3):
    """Bedrock keeps generating after a client timeout; a resend is billed again."""
    _, made = fake_boto3
    monkeypatch.setenv("MAINT_LLM_MODEL", "bedrock/global.openai.gpt-6-luna")
    llm_client.make_complete()
    config = made["config"]
    assert config.read_timeout >= 300
    assert config.retries["total_max_attempts"] == 1


def test_anthropic_native_haiku55_with_thinking_and_usage(monkeypatch):
    monkeypatch.setenv("MAINT_LLM_MODEL", "anthropic/claude-haiku-5-5")
    monkeypatch.setenv("MAINT_LLM_API_KEY", "fake-test-key")
    monkeypatch.setenv("MAINT_LLM_BASE_URL", "https://old.example/v1")
    calls = []

    def post(url, **kw):
        calls.append((url, kw))
        return httpx.Response(200, request=httpx.Request("POST", url), json={
            "content": [{"type": "thinking", "thinking": "ignored"},
                        {"type": "text", "text": "one"}, {"type": "text", "text": "two"}],
            "stop_reason": "end_turn", "usage": {"input_tokens": 7, "output_tokens": 2}})

    monkeypatch.setattr(llm_client.httpx, "post", post)
    usage = []
    assert llm_client.make_complete(on_usage=usage.append)("hello") == "onetwo"
    url, kw = calls[0]
    assert url == "https://api.anthropic.com/v1/messages"
    assert kw["headers"] == {"x-api-key": "fake-test-key", "anthropic-version": "2023-06-01"}
    assert kw["json"] == {"model": "claude-haiku-5-5", "max_tokens": 8192,
                          "messages": [{"role": "user", "content": "hello"}]}
    assert usage == [{"prompt_tokens": 7, "completion_tokens": 2}]


@pytest.mark.parametrize("stop,content", [
    ("max_tokens", [{"type": "text", "text": "partial"}]),
    ("refusal", [{"type": "text", "text": "declined"}]),
    ("end_turn", [{"type": "thinking", "thinking": "only thoughts"}]),
    ("end_turn", [{"type": "text", "text": " "}]),
])
def test_anthropic_incomplete_answer_raises(monkeypatch, stop, content):
    monkeypatch.setattr(llm_client.httpx, "post", lambda url, **kw: httpx.Response(
        200, request=httpx.Request("POST", url), json={"stop_reason": stop, "content": content}))
    with pytest.raises(RuntimeError, match="no complete answer"):
        llm_client.make_complete(model="anthropic/claude-haiku-5-5", api_key="fake-test-key")("hello")


def test_anthropic_explicit_empty_key_does_not_use_maintenance_key(monkeypatch):
    monkeypatch.setenv("MAINT_LLM_API_KEY", "must-not-be-used")
    with pytest.raises(ValueError, match="MAINT_LLM_API_KEY"):
        llm_client.make_complete(model="anthropic/claude-haiku-5-5", api_key="")


def test_anthropic_timeout_is_not_retried(monkeypatch):
    calls = []

    def post(*a, **kw):
        calls.append(a)
        raise httpx.ReadTimeout("timeout")

    monkeypatch.setattr(llm_client.httpx, "post", post)
    with pytest.raises(httpx.ReadTimeout):
        llm_client.make_complete(model="anthropic/claude-haiku-5-5", api_key="fake-test-key")("hello")
    assert len(calls) == 1


@pytest.mark.parametrize("model,url,key,expected", [
    ("anthropic/claude-haiku-5-5", "", "fake-test-key", "llm"),
    ("anthropic/claude-haiku-5-5", "", "", "heuristic"),
    ("ollama", "http://local/v1", "", "llm"),
    ("bedrock/x", "", "", "heuristic"),
])
def test_workflow_selects_native_anthropic_assessor(model, url, key, expected):
    import os
    import subprocess
    from pathlib import Path
    source = (Path(__file__).parents[2] / ".github/workflows/dependency-triage.yml").read_text()
    start = source.index('          assessor="$MAINT_ASSESSOR"')
    end = source.index('          uv run dak-maint triage', start)
    result = subprocess.run(["bash", "-eu", "-c", source[start:end]], capture_output=True, text=True,
                            env={**os.environ, "MAINT_ASSESSOR": "", "MAINT_LLM_MODEL": model,
                                 "MAINT_LLM_BASE_URL": url, "MAINT_LLM_API_KEY": key})
    assert result.returncode == 0
    assert result.stdout.strip() == f"assessor={expected}"
