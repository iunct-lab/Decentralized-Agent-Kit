"""Deterministic OpenAI-compatible fake LLM for integration tests.

Tests enqueue scripted responses per model name via the control API; the
agent under test is pointed here with MODEL_NAME=openai/<model> and
OPENAI_API_BASE=http://fake-llm:8080/v1.

Scripted response items:
    {"text": "..."}                                  -> assistant text message
    {"tool_call": {"name": "...", "args": {...}}}     -> single tool call

When a model's queue is empty, a canned text response is returned so the
agent loop always terminates.
"""
import json
import time
import uuid
from collections import defaultdict, deque
from typing import Any, Dict, List

from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

app = FastAPI()

DEFAULT_TEXT = "FAKE_LLM_DEFAULT_RESPONSE"

# Scripted responses keyed by model name (without provider prefix).
_scripts: Dict[str, deque] = defaultdict(deque)
# Log of received chat requests per model, for debugging from tests.
_requests_log: Dict[str, List[dict]] = defaultdict(list)


class Script(BaseModel):
    responses: List[Dict[str, Any]]


def _normalize_model(model: str) -> str:
    return model.split("/", 1)[-1]


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/script/{model}")
def set_script(model: str, script: Script):
    queue = _scripts[_normalize_model(model)]
    for item in script.responses:
        queue.append(item)
    return {"queued": len(queue)}


@app.delete("/script/{model}")
def clear_script(model: str):
    _scripts[_normalize_model(model)].clear()
    _requests_log[_normalize_model(model)].clear()
    return {"queued": 0}


@app.get("/requests/{model}")
def get_requests(model: str):
    return _requests_log[_normalize_model(model)]


def _build_message(item: Dict[str, Any]) -> (dict, str):
    """Convert a scripted item into an OpenAI assistant message + finish_reason."""
    if "tool_call" in item or "tool_calls" in item:
        calls = item.get("tool_calls") or [item["tool_call"]]  # several = parallel calls in one response
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "index": i,  # tells streamed parallel calls apart
                    "id": f"call_{uuid.uuid4().hex[:8]}",
                    "type": "function",
                    "function": {
                        "name": call["name"],
                        "arguments": json.dumps(call.get("args", {})),
                    },
                }
                for i, call in enumerate(calls)
            ],
        }
        return message, "tool_calls"
    return {"role": "assistant", "content": item.get("text", DEFAULT_TEXT)}, "stop"


@app.post("/v1/chat/completions")
async def chat_completions(body: dict):
    model = _normalize_model(body.get("model", "unknown"))
    _requests_log[model].append({"messages": body.get("messages", []), "tools": body.get("tools")})

    queue = _scripts[model]
    item = queue.popleft() if queue else {"text": DEFAULT_TEXT}
    message, finish_reason = _build_message(item)

    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())
    usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}

    if body.get("stream"):
        # Single-chunk SSE stream
        delta_chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": message, "finish_reason": None}],
        }
        final_chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
            "usage": usage,
        }

        async def stream():
            yield f"data: {json.dumps(delta_chunk)}\n\n"
            yield f"data: {json.dumps(final_chunk)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    return JSONResponse(
        {
            "id": completion_id,
            "object": "chat.completion",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
            "usage": usage,
        }
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
