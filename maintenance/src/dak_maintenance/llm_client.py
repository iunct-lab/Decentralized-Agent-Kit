"""Provider-neutral `complete(prompt) -> str`, selectable at runtime.

Every major provider exposes an OpenAI-compatible /chat/completions endpoint, so
one httpx call covers all of them — no litellm, no hardcoded vendor. The one
SDK is boto3, for Bedrock with IAM (SigV4 signing), imported only on that path.
Pick a provider purely via env:

  MAINT_LLM_BASE_URL   OpenAI-compatible base URL
  MAINT_LLM_MODEL      model id
  MAINT_LLM_API_KEY    api key ("ollama" / anything for keyless local)

Presets (set BASE_URL/MODEL accordingly; prices and the comparison are in
docs/maintenance/model-choice.md, checked 2026-10-01):
  Gemini  https://generativelanguage.googleapis.com/v1beta/openai   gemini-3.5-flash-lite   (GOOGLE_API_KEY)
  Ollama  http://localhost:11434/v1                                 llama3.1:8b             (any key)
  OpenAI  https://api.openai.com/v1                                 gpt-6-luna              (OPENAI_API_KEY)

Amazon Bedrock with IAM (no API key): MAINT_LLM_MODEL=bedrock/<model or
inference profile id>, e.g. bedrock/global.openai.gpt-6-luna. Calls the
Converse API with the environment's AWS credentials (in GitHub Actions: an
OIDC-assumed role); region from AWS_REGION (default us-east-1).
"""

from __future__ import annotations

import os

import httpx


BEDROCK_PREFIX = "bedrock/"


# Reasoning models can think for minutes; Bedrock keeps generating (and billing)
# after a client times out, so wait long and never resend on our own.
BEDROCK_READ_TIMEOUT_S = 300


def _make_bedrock_complete(model_id: str, timeout: float, on_usage=None):
    import boto3  # deferred: only the Bedrock path needs it
    from botocore.config import Config

    region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-east-1"
    client = boto3.client("bedrock-runtime", region_name=region,
                          config=Config(read_timeout=max(timeout, BEDROCK_READ_TIMEOUT_S),
                                        retries={"total_max_attempts": 1}))

    def complete(prompt: str) -> str:
        resp = client.converse(modelId=model_id, messages=[{"role": "user", "content": [{"text": prompt}]}])
        usage = resp.get("usage")
        if on_usage and usage:
            on_usage({"prompt_tokens": usage.get("inputTokens"), "completion_tokens": usage.get("outputTokens")})
        # Reasoning models also return reasoningContent blocks; the answer is the text.
        text = "".join(block.get("text", "") for block in resp["output"]["message"]["content"])
        stop = resp.get("stopReason")
        if stop != "end_turn" or not text.strip():
            # Truncated, filtered or empty: an error, not "the model proposed nothing".
            raise RuntimeError(f"Bedrock {model_id} gave no complete answer (stopReason={stop}, {len(text)} chars)")
        return text

    return complete


def make_complete(timeout: float = 60.0, *, base_url: str | None = None, model: str | None = None,
                  api_key: str | None = None, on_usage=None):
    """Return a `complete(prompt) -> str`, or None if MAINT_LLM_* is not configured.

    base_url / model / api_key override MAINT_LLM_* (to compare models side by side).
    on_usage, if given, is called with {"prompt_tokens", "completion_tokens"} after
    each call whose response reports usage.
    """
    base_url = base_url or os.getenv("MAINT_LLM_BASE_URL")
    model = model or os.getenv("MAINT_LLM_MODEL")
    if model and model.startswith(BEDROCK_PREFIX):
        return _make_bedrock_complete(model[len(BEDROCK_PREFIX):], timeout, on_usage)
    if not base_url or not model:
        return None
    api_key = api_key or os.getenv("MAINT_LLM_API_KEY", "not-needed")

    def complete(prompt: str) -> str:
        resp = httpx.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        usage = data.get("usage")
        if on_usage and usage:
            on_usage({"prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens")})
        return data["choices"][0]["message"]["content"]

    return complete


# Backwards-compatible alias (earlier name).
make_openai_complete = make_complete
