"""LLM helper for the mode-switching Meta-Agent.

Uses LiteLLM so the Meta-Agent talks to the same provider as the main agent
(Gemini, OpenAI, Anthropic, local, or the fake LLM used in integration tests).
"""
import json
import logging
import re
from typing import Any, Dict

logger = logging.getLogger(__name__)

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def strip_think_tags(text: str) -> str:
    """Drop the <think>…</think> reasoning blocks some models (Qwen, DeepSeek) emit before the answer."""
    return _THINK_BLOCK_RE.sub("", text)


def extract_json(text: str) -> Dict[str, Any]:
    """Parse a JSON object out of an LLM response, tolerating surrounding prose."""
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = _JSON_BLOCK_RE.search(text)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    logger.warning("Could not extract JSON from LLM response.")
    return {}


def complete_json(model_name: str, prompt: str) -> Dict[str, Any]:
    """Run a single completion and return the parsed JSON object ({} on failure)."""
    import litellm

    messages = [{"role": "user", "content": prompt}]
    try:
        response = litellm.completion(
            model=model_name,
            messages=messages,
            response_format={"type": "json_object"},
        )
    except Exception as e:
        logger.info(f"JSON-mode completion failed ({e}); retrying without response_format.")
        try:
            response = litellm.completion(model=model_name, messages=messages)
        except Exception as e2:
            logger.error(f"Meta-LLM completion failed: {e2}")
            return {}

    text = response.choices[0].message.content or ""
    return extract_json(strip_think_tags(text))
