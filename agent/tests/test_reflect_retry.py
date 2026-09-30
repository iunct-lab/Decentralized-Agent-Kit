"""PBI #91: compare ADK's ReflectAndRetryToolPlugin with DAK's own handling of tool failures.

A scripted model plays the worst small model: it repeats the same failing call
(up to MAX_CALLS) whatever the observation says, and stops only on a success
or when it runs out. Arms, all on DAK's AdaptiveAgent (its ``_on_tool_error``
turns an exception into an ``{"error": ...}`` observation):

- ``adaptive``: no plugin
- ``harness``: ContextHarnessPlugin, DAK's default (repeated-call guard)
- ``reflect``: ReflectAndRetryToolPlugin(max_retries=3), ADK's defaults
- ``reflect_no_throw``: the same with throw_exception_if_retry_exceeded=False

Scenarios: ``always_fails`` (the tool never succeeds) and ``transient`` (it
fails 3 times, then succeeds). The numbers are recorded in #218 and
docs/design/reflect_retry_plugin.md.
"""
from unittest.mock import MagicMock

import pytest
from google.adk.plugins.reflect_retry_tool_plugin import ReflectAndRetryToolPlugin
from google.genai import types

from dak_agent import harness
from dak_agent.errors import PaymentRequiredError
from dak_agent.harness import ContextHarnessPlugin, HarnessSettings

MAX_CALLS = 20
TRANSIENT_FAILURES = 3


def _plugins(arm):
    return {
        "adaptive": [],
        "harness": [ContextHarnessPlugin(HarnessSettings(context_window=32_000), "test-model")],
        "reflect": [ReflectAndRetryToolPlugin(max_retries=3)],
        "reflect_no_throw": [ReflectAndRetryToolPlugin(max_retries=3, throw_exception_if_retry_exceeded=False)],
    }[arm]


def _make_llm():
    from google.adk.models.base_llm import BaseLlm
    from google.adk.models.llm_response import LlmResponse

    class ScriptedLlm(BaseLlm):
        """Calls flaky_tool with the same (empty) arguments until a call succeeds or MAX_CALLS were made."""
        calls: int = 0
        request_tokens: list = []

        async def generate_content_async(self, llm_request, stream=False):
            self.request_tokens.append(harness._fixed_request_tokens(llm_request) + sum(
                harness._content_tokens(c) for c in llm_request.contents or []))
            last = llm_request.contents[-1].parts[-1].function_response
            if (last and last.response == {"result": "ok"}) or self.calls >= MAX_CALLS:
                part = types.Part(text="done")
            else:
                self.calls += 1
                part = types.Part(function_call=types.FunctionCall(id=f"fc-{self.calls}", name="flaky_tool", args={}))
            yield LlmResponse(content=types.Content(role="model", parts=[part]))

    return ScriptedLlm(model="scripted")


async def _run(arm, failures, error=ValueError("boom"), ap2=False):
    """Run one user turn. `failures`: how many calls fail before the tool succeeds."""
    from google.adk.apps import App
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.adk.tools import FunctionTool

    from dak_agent.adaptive_agent import AdaptiveAgent

    runs = []

    def flaky_tool() -> str:
        """Fetch the report."""
        runs.append(1)
        if len(runs) <= failures:
            raise error
        return "ok"

    llm = _make_llm()
    agent = AdaptiveAgent(model=llm, name="dak_agent", instruction="Fetch the report.",
                          tools=[FunctionTool(flaky_tool)], disable_mode_switching=True)
    if ap2:
        agent._enable_ap2 = True
        agent._payment_handler = MagicMock()
        agent._payment_handler.format_payment_error.return_value = {"observation": "payment_required"}
    app = App(name="dak_agent", root_agent=agent, plugins=_plugins(arm))
    sessions = InMemorySessionService()
    runner = Runner(app=app, session_service=sessions)
    session = await sessions.create_session(app_name="dak_agent", user_id="u")
    responses, final_text, aborted = [], None, None
    try:
        async for event in runner.run_async(user_id="u", session_id=session.id, new_message=types.Content(
                role="user", parts=[types.Part(text="レポートを取ってきて")])):
            for part in (event.content.parts if event.content else None) or []:
                if part.function_response:
                    responses.append(part.function_response.response)
                elif part.text:
                    final_text = part.text
    except Exception as e:  # noqa: BLE001 - what the plugin's throw turns into
        aborted = e
    return {
        "tool_runs": len(runs),
        "model_requests": len(llm.request_tokens),
        "tokens": sum(llm.request_tokens),
        "success": {"result": "ok"} in responses,
        "aborted": aborted,
        "final_text": final_text,
        "responses": responses,
    }


@pytest.mark.asyncio
async def test_without_plugin_retries_unbounded_by_design():
    """Only the model's own budget stops it: every one of the MAX_CALLS calls runs the tool."""
    r = await _run("adaptive", failures=MAX_CALLS)
    assert (r["tool_runs"], r["model_requests"], r["aborted"], r["final_text"]) == (MAX_CALLS, MAX_CALLS + 1, None, "done")
    assert all("failed: boom" in resp["error"] for resp in r["responses"])


@pytest.mark.asyncio
async def test_with_plugin_stops_at_max_retries():
    """ADK's default ends the invocation on the 4th failure (1 + max_retries): the plugin re-raises the
    tool's exception and ADK's plugin manager wraps it in a RuntimeError that leaves Runner.run_async.
    Without the throw, the plugin only says "do not call again"; a model that ignores it goes on."""
    r = await _run("reflect", failures=MAX_CALLS)
    assert (r["tool_runs"], r["model_requests"]) == (4, 4)
    assert isinstance(r["aborted"], RuntimeError) and "boom" in str(r["aborted"]) and not r["success"]
    assert r["final_text"] is None  # the user gets no answer
    assert [resp["retry_count"] for resp in r["responses"]] == [1, 2, 3]

    r = await _run("reflect_no_throw", failures=MAX_CALLS)
    assert (r["tool_runs"], r["aborted"], r["final_text"]) == (MAX_CALLS, None, "done")
    assert "failed consecutively 3 times" in r["responses"][-1]["reflection_guidance"]


@pytest.mark.asyncio
async def test_comparison_table():
    """Same model, same tool, per arm: tool runs / model requests / success / aborted, and tokens sent."""
    expected = {
        ("always_fails", "adaptive"): (20, 21, False, False),
        ("always_fails", "harness"): (3, 21, False, False),       # 4th identical call on is blocked, not run
        ("always_fails", "reflect"): (4, 4, False, True),
        ("always_fails", "reflect_no_throw"): (20, 21, False, False),
        ("transient", "adaptive"): (4, 5, True, False),
        ("transient", "harness"): (3, 21, False, False),          # the call that would succeed is blocked
        ("transient", "reflect"): (4, 5, True, False),
        ("transient", "reflect_no_throw"): (4, 5, True, False),
    }
    tokens, lines = {}, ["| scenario | arm | tool runs | model requests | tokens sent | success | aborted |"]
    for (scenario, arm), want in expected.items():
        r = await _run(arm, failures=MAX_CALLS if scenario == "always_fails" else TRANSIENT_FAILURES)
        got = (r["tool_runs"], r["model_requests"], r["success"], r["aborted"] is not None)
        assert got == want, (scenario, arm)
        assert r["final_text"] == (None if got[3] else "done"), (scenario, arm)  # an answer unless aborted
        tokens[scenario, arm] = r["tokens"]
        lines.append(f"| {scenario} | {arm} | {got[0]} | {got[1]} | {r['tokens']} | {got[2]} | {got[3]} |")
    print("\n" + "\n".join(lines))

    # The reflection guidance is ~10x an error observation: per request it costs more context,
    # so only stopping early (the throw) makes the plugin cheaper than no plugin.
    assert tokens["always_fails", "reflect"] < tokens["always_fails", "adaptive"]
    assert tokens["always_fails", "reflect_no_throw"] > tokens["always_fails", "adaptive"]
    assert tokens["transient", "reflect"] > tokens["transient", "adaptive"]


@pytest.mark.asyncio
async def test_plugin_pre_empts_ap2_payment_observation():
    """ADK runs plugin on_tool_error callbacks before the agent's and skips the agent's once a plugin
    answers, so with the plugin a PaymentRequiredError never reaches AdaptiveAgent._on_tool_error."""
    payment = PaymentRequiredError(price=1.0, address="addr", message="fee")

    r = await _run("adaptive", failures=1, error=payment, ap2=True)
    assert r["responses"][0] == {"observation": "payment_required"}

    r = await _run("reflect", failures=1, error=payment, ap2=True)
    assert r["responses"][0]["error_type"] == "PaymentRequiredError"
    assert "observation" not in r["responses"][0]
