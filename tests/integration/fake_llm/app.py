"""Deterministic OpenAI-compatible fake LLM for integration tests.

Tests enqueue scripted responses per model name via the control API; the
agent under test is pointed here with MODEL_NAME=openai/<model> and
OPENAI_API_BASE=http://fake-llm:8080/v1.

Scripted response items:
    {"text": "..."}                                  -> assistant text message
    {"tool_call": {"name": "...", "args": {...}}}     -> single tool call
    {"thinking": "...", ...one of the above}          -> same, with a signed
        thinking block first (POST /v1/messages only; signature "sig-<n>")

When a model's queue is empty, a canned text response is returned so the
agent loop always terminates.

GET /requests/{model} returns each received request as {"messages": [...],
"tools": [...]} (the tool definitions; [] when none were sent), oldest first;
DELETE /script/{model} clears them with the script.

POST /v1/messages is the Anthropic Messages API (non-streaming only; DAK never
streams). Its requests are logged as {"system", "tools", "messages"} in the
Anthropic shape. It enforces Claude 5.5's rule: a request whose system, tools
or earlier turns changed since the previous request gets a 400 when it sends
back a signed thinking block. "Previous" is per model and per has-tools: a call
without tools (a mode switch's meta call, a compaction summary) is a different
conversation from the agent's own. A rejected request still becomes the
previous one.
"""
import json
import time
import uuid
from collections import defaultdict, deque
from typing import Any, Dict, List, Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

app = FastAPI()

DEFAULT_TEXT = "FAKE_LLM_DEFAULT_RESPONSE"

# Scripted responses keyed by model name (without provider prefix).
_scripts: Dict[str, deque] = defaultdict(deque)
# Log of received chat requests per model, for debugging from tests.
_requests_log: Dict[str, List[dict]] = defaultdict(list)
# The previous /v1/messages request per (model, has tools), for the
# thinking-resend rule. Calls without tools (the mode switch's meta call, the
# compaction summarizer) are a different conversation from the agent's own.
_last_anthropic_request: Dict[tuple, dict] = {}
_signature_counter = 0


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
    for has_tools in (True, False):
        _last_anthropic_request.pop((_normalize_model(model), has_tools), None)
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


def _build_anthropic_message(item: Dict[str, Any]) -> (List[dict], str):
    """Convert a scripted item into Anthropic content blocks + stop_reason."""
    global _signature_counter
    content: List[dict] = []
    if item.get("thinking"):
        _signature_counter += 1
        content.append({"type": "thinking", "thinking": item["thinking"], "signature": f"sig-{_signature_counter}"})
    if "tool_call" in item or "tool_calls" in item:
        for call in item.get("tool_calls") or [item["tool_call"]]:
            content.append({
                "type": "tool_use",
                "id": f"toolu_{uuid.uuid4().hex[:8]}",
                "name": call["name"],
                "input": call.get("args", {}),
            })
        return content, "tool_use"
    content.append({"type": "text", "text": item.get("text", DEFAULT_TEXT)})
    return content, "end_turn"


def _has_signed_thinking(messages: List[dict]) -> bool:
    for message in messages:
        blocks = message.get("content")
        if message.get("role") != "assistant" or not isinstance(blocks, list):
            continue
        if any(b.get("type") == "thinking" and b.get("signature") for b in blocks if isinstance(b, dict)):
            return True
    return False


def _thinking_resend_error(model: str, body: dict) -> Optional[str]:
    """The 400 message when a signed thinking block comes back after the
    request's system, tools or earlier turns changed; else None."""
    previous = _last_anthropic_request.get((model, bool(body.get("tools"))))
    if previous is None:
        return None
    messages = body.get("messages", [])
    changed = (
        body.get("system") != previous["system"]
        or body.get("tools", []) != previous["tools"]
        or messages[: len(previous["messages"])] != previous["messages"]
    )
    if changed and _has_signed_thinking(messages):
        return "thinking blocks cannot be sent after earlier parts of the request changed"
    return None


@app.post("/v1/messages")
async def anthropic_messages(body: dict):
    model = _normalize_model(body.get("model", "unknown"))
    system, tools, messages = body.get("system"), body.get("tools", []), body.get("messages", [])
    _requests_log[model].append({"system": system, "tools": tools, "messages": messages})

    error = _thinking_resend_error(model, body)
    _last_anthropic_request[(model, bool(tools))] = {"system": system, "tools": tools, "messages": messages}
    if error:
        return JSONResponse(
            {"type": "error", "error": {"type": "invalid_request_error", "message": error}},
            status_code=400,
        )

    queue = _scripts[model]
    item = queue.popleft() if queue else {"text": DEFAULT_TEXT}
    content, stop_reason = _build_anthropic_message(item)
    return JSONResponse({
        "id": f"msg_{uuid.uuid4().hex[:12]}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    })


@app.post("/v1/chat/completions")
async def chat_completions(body: dict):
    model = _normalize_model(body.get("model", "unknown"))
    _requests_log[model].append({"messages": body.get("messages", []), "tools": body.get("tools", [])})

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
