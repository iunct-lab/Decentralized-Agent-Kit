"""The agent container exports GenAI-convention spans (invoke_agent, a model call,
execute_tool) to the OTLP endpoint set by OTEL_EXPORTER_OTLP_ENDPOINT (docker-compose.test.yml
points it at otlp-sink). No LangFuse key is set; message content is off by default."""
import time

import httpx

from conftest import AGENT_RUN_TIMEOUT, AGENT_URL, APP_NAME, OTLP_SINK_URL

MODEL = "fake-default"
REPLY_MARKER = "otlp-reply-marker-7f3a"


def _spans_of_run() -> list:
    deadline = time.time() + 20
    spans = []
    while time.time() < deadline:
        spans = httpx.get(f"{OTLP_SINK_URL}/spans", timeout=10.0).json()
        ops = {s["attributes"].get("gen_ai.operation.name") for s in spans}
        if "execute_tool" in ops and "invoke_agent" in ops and ops & {"chat", "generate_content"}:
            break
        time.sleep(0.5)
    return spans


def test_genai_spans(agent, fake_llm):
    httpx.delete(f"{OTLP_SINK_URL}/spans", timeout=10.0)
    fake_llm.clear(MODEL)
    fake_llm.script(MODEL, [fake_llm.tool_call("run_command", command="git status"), fake_llm.text(REPLY_MARKER)])
    payload = {
        "app_name": APP_NAME,
        "user_id": agent.user_id,
        "session_id": agent.create_session(),
        "new_message": {"parts": [{"text": "run it"}]},
        "state_delta": {"dak:tools": ["run_command"]},
    }
    resp = httpx.post(f"{AGENT_URL}/run", json=payload, timeout=AGENT_RUN_TIMEOUT)
    resp.raise_for_status()

    spans = _spans_of_run()
    [tool] = [s for s in spans if s["attributes"].get("gen_ai.operation.name") == "execute_tool"
              and s["attributes"].get("gen_ai.tool.name") == "run_command"]
    # only this run's trace: spans of an earlier run may still arrive
    run = [s for s in spans if s["trace_id"] == tool["trace_id"]]
    ops = {s["attributes"].get("gen_ai.operation.name"): s for s in run}
    assert "invoke_agent" in ops, f"spans: {[s['name'] for s in spans]}"
    model_calls = [s for s in run if s["attributes"].get("gen_ai.operation.name") in ("chat", "generate_content")]
    assert model_calls, f"spans: {[s['name'] for s in spans]}"
    assert all(s["attributes"].get("gen_ai.request.model") for s in model_calls)
    # content is opt-in: neither the reply nor the arguments are in any attribute
    values = [str(v) for s in run for v in s["attributes"].values()]
    assert not [v for v in values if REPLY_MARKER in v or "git status" in v]
