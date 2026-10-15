"""The Lambda image (agent/Dockerfile.lambda) through the runtime interface
emulator: one turn per invocation, read-only root file system, no session
database. PBI #135 acceptance criteria 1 and 3 (in the image)."""
import json

import httpx

from conftest import AGENT_LAMBDA_URL, AGENT_RUN_TIMEOUT, wait_for

MODEL = "fake-lambda"
DATE_SCHEMA = {"type": "object", "required": ["date"], "properties": {"date": {"type": "string"}}}
INVOKE = f"{AGENT_LAMBDA_URL}/2015-03-31/functions/function/invocations"


def _invoke(event: dict) -> dict:
    wait_for(AGENT_LAMBDA_URL)
    resp = httpx.post(INVOKE, json=event, timeout=AGENT_RUN_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def test_one_turn_returns_output_matching_the_schema(fake_llm):
    fake_llm.clear(MODEL)
    fake_llm.script(MODEL, [fake_llm.text('{"date": "2026-10-14"}')])

    result = _invoke({
        "message": "when?",
        "state_delta": {"dak:instruction": "Answer with JSON.", "dak:output_schema": DATE_SCHEMA},
    })

    assert result["status"] == "succeeded", result
    assert json.loads(result["output"]) == {"date": "2026-10-14"}


def test_invalid_event_is_a_failed_result_without_calling_the_llm(fake_llm):
    fake_llm.clear(MODEL)

    assert _invoke({"message": 1}) == {"status": "failed", "error": "invalid_event"}
    assert fake_llm.requests(MODEL) == []
