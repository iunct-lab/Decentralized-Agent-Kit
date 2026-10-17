"""HTTP entry point: ADK's FastAPI app plus `/approvals` (docs/design/approval-queue.md).

`adk web` used to build the app itself; this builds the same app with
`get_fast_api_app` and adds three routes on top:

- `GET  /approvals`            pending approvals and questions of a session
- `POST /approvals/{id}/reply` answer one (once / always / reject, or a question's answer)
- `GET  /approvals/stream`     the same list as server-sent events

Nothing is stored here. The routes read the session and resume it through
ADK's own REST routes (`GET /apps/.../sessions/...`, `POST /run`), called
in-process, so any client that answers gets the same result.

Every request is logged as one `request` line (`request_log.py`).

A middleware refuses (400) a session `state` / `state_delta` that sets a key DAK
owns (`permission.owned_state_keys`), so a caller cannot pre-approve a tool.
"""
import asyncio
import json
import os
import time
import weakref
from typing import Literal, Optional
from urllib.parse import quote

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from google.adk.cli.fast_api import get_fast_api_app
from opentelemetry import trace
from pydantic import BaseModel

from . import agent_card, approvals, config, patches, request_log
from .permission import owned_state_keys

# The directory holding `dak_agent/` (what `adk web` got as its working directory).
AGENTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STREAM_INTERVAL_SECONDS = 2.0

# ADK reads agent.json while building the app, so the skills and the signature go in first.
# Skills before the signature: the signature covers them too.
CARD_PATH = os.path.join(AGENTS_DIR, "dak_agent", "agent.json")
agent_card.add_skills_in_place(CARD_PATH, config.skills_dirs_from_env())
agent_card.sign_card_from_env(CARD_PATH)

app = get_fast_api_app(
    agents_dir=AGENTS_DIR,
    session_service_uri=os.environ["SESSION_SERVICE_URI"],
    allow_origins=os.getenv("DAK_ALLOW_ORIGINS", "http://localhost:3000").split(","),
    web=True,
    a2a=True,
    host="0.0.0.0",
    port=8000,
)
# So the first request also gets a real trace ID, whichever module loads first.
patches.ensure_tracer_provider()



def _delta(body: dict) -> dict:
    """`state_delta` under both names ADK's request models accept (`stateDelta` too); only dicts count."""
    return {k: v for name in ("state_delta", "stateDelta") if isinstance(body.get(name), dict) for k, v in body[name].items()}


def _caller_state(request: Request, body: object) -> object:
    """Where a request carries session state the caller wrote (None: not one)."""
    if not isinstance(body, dict):
        return None
    path, method = request.url.path.rstrip("/"), request.method
    parts = path.split("/")
    in_session = len(parts) >= 6 and parts[1] == "apps" and parts[3] == "users" and parts[5] == "sessions"
    if method == "POST" and path in ("/run", "/run_sse"):
        return _delta(body)
    if in_session and len(parts) == 6 and method == "POST":
        return body.get("state")
    if in_session and len(parts) == 7 and method == "POST":
        return body
    if in_session and len(parts) == 7 and method == "PATCH":
        return _delta(body)
    return None


@app.middleware("http")
async def reject_owned_state(request: Request, call_next):
    if request.method in ("POST", "PATCH"):
        try:
            state = _caller_state(request, json.loads(await request.body()))
        except ValueError:
            state = None  # not JSON: ADK answers 422
        if isinstance(state, dict):
            keys = owned_state_keys(state)
            if keys:
                return JSONResponse(
                    status_code=400,
                    content={"detail": f"state keys owned by DAK cannot be set by the caller: {keys}"},
                )
    return await call_next(request)


router = APIRouter(prefix="/approvals")

# One reply at a time per session: two replies to the same id must not both
# pass the pending check (ADK itself would run the tool twice). Held only by
# a running reply, so an idle session's lock is collected.
_session_locks: "weakref.WeakValueDictionary[tuple, asyncio.Lock]" = weakref.WeakValueDictionary()


def _adk() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("in-process", 0)), base_url="http://adk", timeout=None)


async def _session_events(client: httpx.AsyncClient, app_name: str, user_id: str, session_id: str) -> list[dict]:
    # Each id is one path segment: `s1/../s2` must not reach another session
    app_name, user_id, session_id = (quote(s, safe="") for s in (app_name, user_id, session_id))
    resp = await client.get(f"/apps/{app_name}/users/{user_id}/sessions/{session_id}")
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    return resp.json().get("events") or []


async def _pending(client: httpx.AsyncClient, app_name: str, user_id: str, session_id: str) -> list[dict]:
    events = await _session_events(client, app_name, user_id, session_id)
    return [
        {**p, "session_id": session_id,
         "status": "timed_out" if approvals.is_expired(p["requested_at"]) else "pending"}
        for p in approvals.list_pending(events)
    ]


@router.get("")
async def list_approvals(user_id: str, session_id: str, app_name: str = "dak_agent") -> list[dict]:
    """Expired items are shown as `timed_out`, not consumed: consuming one
    runs the agent, which a GET must not do."""
    async with _adk() as client:
        return await _pending(client, app_name, user_id, session_id)


class Reply(BaseModel):
    user_id: str
    session_id: str
    app_name: str = "dak_agent"
    mode: Optional[Literal["once", "always", "reject"]] = None  # approvals
    reason: str = ""
    answer: Optional[str] = None  # questions


@router.post("/{approval_id}/reply")
async def reply(approval_id: str, body: Reply):
    """Resume the session with the answer and return ADK's events.

    404: the id is not pending (answered, dropped by a newer message, unknown).
    409: the approval had expired. The agent was still resumed with a
    `timed_out` answer, so the model has moved on; do not answer again.
    """
    key = (body.app_name, body.user_id, body.session_id)
    lock = _session_locks.setdefault(key, asyncio.Lock())
    async with lock, _adk() as client:
        pending = {p["id"]: p for p in await _pending(client, *key)}
        item = pending.get(approval_id)
        if item is None:
            raise HTTPException(status_code=404, detail=f"{approval_id} is not pending")

        timed_out = False
        if item["kind"] == "question":
            if body.answer is None:
                raise HTTPException(status_code=422, detail="a question needs an answer")
            message = approvals.build_question_reply(body.answer)
        else:
            if body.mode is None:
                raise HTTPException(status_code=422, detail="an approval needs a mode")
            timed_out = item["status"] == "timed_out"
            unanswered = [(pid, p["status"]) for pid, p in pending.items()
                          if pid != approval_id and p["kind"] == "approval"]
            message = approvals.build_reply_function_response(
                approval_id, "timed_out" if timed_out else body.mode, "" if timed_out else body.reason, unanswered)

        resp = await client.post("/run", json={
            "app_name": body.app_name, "user_id": body.user_id,
            "session_id": body.session_id, "new_message": message,
        })
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail=resp.text)
        if timed_out:
            return JSONResponse(status_code=409, content={"observation": "timed_out"})
        return resp.json()


def _stream_events(seen: dict[str, dict], current: dict[str, dict]) -> tuple[list[str], dict[str, dict]]:
    """The events between two readings of the list, and the new `seen`."""
    events = []
    for pid, item in current.items():
        if pid not in seen:
            events.append(f"event: approval.asked\ndata: {json.dumps(item)}\n\n")
        elif item["status"] == "timed_out" and seen[pid]["status"] != "timed_out":
            events.append(f"event: approval.timed_out\ndata: {json.dumps(item)}\n\n")
    for pid in seen.keys() - current.keys():
        events.append(f"event: approval.replied\ndata: {json.dumps({'id': pid})}\n\n")
    return events, current


@router.get("/stream")
async def stream(request: Request, user_id: str, session_id: str, app_name: str = "dak_agent"):
    """`approval.asked` for each new pending item, `approval.timed_out` when one
    expires (still listed, see GET), `approval.replied` when one leaves the
    list. Polls every STREAM_INTERVAL_SECONDS until the client goes."""
    async with _adk() as client:  # an unknown session is a 404, not a stream that breaks
        await _session_events(client, app_name, user_id, session_id)

    async def events():
        seen: dict[str, dict] = {}
        async with _adk() as client:
            while not await request.is_disconnected():
                current = {p["id"]: p for p in await _pending(client, app_name, user_id, session_id)}
                batch, seen = _stream_events(seen, current)
                for event in batch:
                    yield event
                await asyncio.sleep(STREAM_INTERVAL_SECONDS)

    return StreamingResponse(events(), media_type="text/event-stream")


app.include_router(router)


# Added last, so it is the outermost middleware and also records the 400 above.
@app.middleware("http")
async def log_request(request: Request, call_next):
    path = request.url.path.rstrip("/") or "/"
    body = None
    if request_log.reads_body(request.method, path):
        try:
            body = json.loads(await request.body())
        except ValueError:
            pass  # not JSON: no body fields
    fields = request_log.request_fields(path, request.query_params, body)
    fields.update(client=request.client.host if request.client else "-", method=request.method, path=path)
    started = time.monotonic()
    # Nothing extracts the caller's `traceparent`, so a caller cannot pick the ID the lines are matched by.
    with trace.get_tracer(__name__).start_as_current_span("dak.request") as span:
        trace_id = span.get_span_context().trace_id
        fields["trace_id"] = format(trace_id, "032x") if trace_id else "-"
        try:
            response = await call_next(request)
            fields["status"] = response.status_code
        except Exception:
            fields["status"] = 500
            raise
        finally:
            fields["ms"] = int((time.monotonic() - started) * 1000)
            request_log.logger.info(request_log.format_line(fields))
    return response
