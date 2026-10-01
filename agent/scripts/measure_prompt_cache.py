"""Measure what ADK's ContextCacheConfig does to tokens and latency on a real model (PBI #94, Task #225).

Sends the same multi-turn conversation (a long instruction, a few tool
definitions, short user turns) through ADK's LiteLlm, once without and once
with `ContextCacheConfig`, and prints one JSON line per model request: input
tokens, cache read / write tokens, output tokens and wall-clock seconds.

    cd agent && uv run python scripts/measure_prompt_cache.py bedrock/<model-id> [--repeats 2] [--api-base URL]

Calls the model for real (it costs money); credentials come from the
environment as for any LiteLLM provider. `--api-base` points it at a stand-in
server instead, to check the script without calling a model.
"""
import argparse
import asyncio
import json
import time

from google.adk.agents import LlmAgent
from google.adk.agents.context_cache_config import ContextCacheConfig
from google.adk.apps import App
from google.adk.models.lite_llm import LiteLlm
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools import FunctionTool
from google.genai import types

# ~6K tokens: above the largest minimum cacheable prefix among Bedrock models (4,096 for Claude Haiku 4.5).
INSTRUCTION = "You are an operations assistant. Answer in one short sentence.\n\n" + "\n".join(
    f"Rule {i}: when a log line mentions service-{i % 37}, check its queue depth, its error rate and "
    f"its last deploy before answering, and never restart it without a ticket numbered {1000 + i}."
    for i in range(160))
TURNS = ["Which rule covers service-3?", "And what ticket does rule 40 need?", "Summarize rule 7 in five words."]
RECORDS: list = []


def read_log(service: str, lines: int = 20) -> str:
    """Return the last lines of a service's log."""
    return ""


def queue_depth(service: str) -> int:
    """Return the current queue depth of a service."""
    return 0


def last_deploy(service: str) -> str:
    """Return when and what was last deployed for a service."""
    return ""


class TimedLiteLlm(LiteLlm):
    """Records each request's usage and wall-clock time."""

    async def generate_content_async(self, llm_request, stream=False):
        start, last = time.perf_counter(), None
        async for response in super().generate_content_async(llm_request, stream=stream):
            last = response
            yield response
        usage = last.usage_metadata if last else None
        RECORDS.append({
            "seconds": round(time.perf_counter() - start, 3),
            "input": usage.prompt_token_count if usage else None,
            "cache_read": usage.cached_content_token_count if usage else None,
            "cache_write": getattr(usage, "cache_creation_input_tokens", None) if usage else None,
            "output": usage.candidates_token_count if usage else None,
        })


async def run(model: str, cache: bool, api_base: str | None) -> list:
    kwargs = {"api_base": api_base, "api_key": "unused"} if api_base else {}
    llm = TimedLiteLlm(model=model, **kwargs)
    agent = LlmAgent(name="ops", model=llm, instruction=INSTRUCTION,
                     tools=[FunctionTool(read_log), FunctionTool(queue_depth), FunctionTool(last_deploy)],
                     generate_content_config=types.GenerateContentConfig(max_output_tokens=64, temperature=0))
    app = App(name="ops", root_agent=agent, context_cache_config=ContextCacheConfig(min_tokens=0) if cache else None)
    sessions = InMemorySessionService()
    runner = Runner(app=app, session_service=sessions)
    session = await sessions.create_session(app_name="ops", user_id="u")
    RECORDS.clear()
    for text in TURNS:
        async for _ in runner.run_async(user_id="u", session_id=session.id,
                                        new_message=types.Content(role="user", parts=[types.Part(text=text)])):
            pass
    return list(RECORDS)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("model")
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--api-base")
    args = parser.parse_args()
    for repeat in range(args.repeats):
        for cache in (False, True) if repeat % 2 == 0 else (True, False):
            for turn, record in enumerate(await run(args.model, cache, args.api_base), 1):
                print(json.dumps({"model": args.model, "cache": cache, "repeat": repeat, "request": turn, **record}),
                      flush=True)


if __name__ == "__main__":
    asyncio.run(main())
