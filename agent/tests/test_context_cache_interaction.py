"""How ADK's ContextCacheConfig meets DAK's compaction and local-LLM routes (PBI #94).

Free checks only: whether the cache setting still reaches every model request
once compaction has rewritten the history, and whether the cache marks LiteLLM
derives from it leave the process on the two local-LLM routes. Whether a
provider then actually hits its cache is measured separately (#225).
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from google.adk.agents import LlmAgent
from google.adk.agents.context_cache_config import ContextCacheConfig
from google.adk.apps import App
from google.adk.artifacts import InMemoryArtifactService
from google.adk.models.base_llm import BaseLlm
from google.adk.models.lite_llm import LiteLlm, LiteLLMClient
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools import FunctionTool
from google.genai import types

from dak_agent.harness import ContextHarnessPlugin, HarnessSettings, make_compaction_config

WINDOW = 8192


def big_tool(page: int = 0) -> str:
    """Return a huge log."""
    return "日本語のログ行です\n" * 4000


class ScriptedLlm(BaseLlm):
    """Calls big_tool `tool_calls` times per user turn, then answers. Records the
    cache config of each agent request with how many summaries preceded it, and
    that of each summarizer request."""
    tool_calls: int = 4
    summaries: int = 0
    turn_steps: int = 0
    agent_requests: list = []
    summary_requests: list = []

    async def generate_content_async(self, llm_request, stream=False):
        text = "".join(p.text or "" for c in llm_request.contents for p in c.parts or [])
        if "compacting the working memory" in text:
            self.summaries += 1
            self.summary_requests.append(llm_request.cache_config)
            yield LlmResponse(content=types.Content(role="model", parts=[types.Part(
                text=f"User request: inspect logs. Progress: read logs (summary {self.summaries}).")]))
            return
        self.agent_requests.append((self.summaries, llm_request.cache_config))
        last = llm_request.contents[-1]
        if last.role == "user" and any(p.text for p in last.parts or []):
            self.turn_steps = 0
        self.turn_steps += 1
        if self.turn_steps <= self.tool_calls:
            part = types.Part(function_call=types.FunctionCall(
                id=f"fc-{len(self.agent_requests)}", name="big_tool", args={"page": len(self.agent_requests)}))
        else:
            part = types.Part(text="done")
        yield LlmResponse(content=types.Content(role="model", parts=[part]))


@pytest.mark.asyncio
async def test_cache_config_survives_compaction():
    """Compaction replaces old events with a summary; the cache config is copied
    from the invocation onto each request regardless, so every agent request
    before and after a summary carries the same config."""
    llm = ScriptedLlm(model="scripted")
    settings = HarnessSettings(context_window=WINDOW)
    cache = ContextCacheConfig(min_tokens=0)
    agent = LlmAgent(name="dak_agent", model=llm, instruction="Inspect the logs.", tools=[FunctionTool(big_tool)])
    app = App(name="dak_agent", root_agent=agent, context_cache_config=cache,
              events_compaction_config=make_compaction_config(settings, llm=llm),
              plugins=[ContextHarnessPlugin(settings, "test-model")])
    sessions = InMemorySessionService()
    runner = Runner(app=app, session_service=sessions, artifact_service=InMemoryArtifactService())
    session = await sessions.create_session(app_name="dak_agent", user_id="u")

    for _ in range(3):
        async for _ in runner.run_async(user_id="u", session_id=session.id, new_message=types.Content(
                role="user", parts=[types.Part(text="ログを全部読んで要約して")])):
            pass

    assert llm.summaries >= 2  # compaction ran, more than once
    assert {n for n, _ in llm.agent_requests} >= {0, 1, 2}  # requests before and after summaries
    assert all(config == cache for _, config in llm.agent_requests)
    # DAK's summarizer builds its own request, so it asks for no cache marks.
    assert llm.summary_requests == [None] * llm.summaries


@pytest.fixture
def local_server():
    """An HTTP server answering both Ollama's /api/chat and the OpenAI-compatible
    /v1/chat/completions, recording each request body."""
    bodies = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            bodies.append(body)
            message = {"role": "assistant", "content": "ok"}
            if self.path.endswith("/api/chat"):
                reply = {"model": "m", "created_at": "2026-01-01T00:00:00Z", "message": message, "done": True}
            else:
                reply = {"id": "1", "object": "chat.completion", "created": 0, "model": "m",
                         "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
                         "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
            data = json.dumps(reply).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", bodies
    server.shutdown()
    server.server_close()


@pytest.mark.asyncio
@pytest.mark.parametrize("model, path, marked", [
    ("ollama_chat/llama3.1:8b", "", False),  # docker-compose.local-llm.yml
    ("openai/llamacpp", "/v1", True),        # docker-compose.llamacpp.yml
])
async def test_local_llm_routes(local_server, model, path, marked):
    """LiteLlm adds cache marks whatever the model. The Ollama route rebuilds each
    message from role and content, so the marks never leave; the OpenAI route to
    a custom host keeps them, and llama-server receives cache_control."""
    base, bodies = local_server
    calls = []

    class RecordingClient(LiteLLMClient):
        async def acompletion(self, **kwargs):
            calls.append(kwargs)
            return await super().acompletion(**kwargs)

    llm = LiteLlm(model=model, api_base=base + path, api_key="unused", llm_client=RecordingClient())
    request = LlmRequest(
        contents=[types.Content(role="user", parts=[types.Part(text="hi")])],
        config=types.GenerateContentConfig(system_instruction="Inspect the logs."),
        cache_config=ContextCacheConfig(min_tokens=0))

    async for _ in llm.generate_content_async(request):
        pass

    assert len(calls[0]["cache_control_injection_points"]) == 2  # marked on both routes
    chat = next(b for b in bodies if b.get("messages"))
    assert ["cache_control" in m for m in chat["messages"]] == [marked, marked]
