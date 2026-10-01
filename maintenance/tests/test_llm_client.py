"""make_complete: OpenAI-compatible HTTP (MAINT_LLM_BASE_URL) or Amazon Bedrock
with IAM credentials (MAINT_LLM_MODEL=bedrock/<model or inference profile>)."""
import sys
import types

import pytest

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
