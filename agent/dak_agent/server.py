"""HTTP entry point: ADK's FastAPI app plus `/approvals` (docs/design/approval-queue.md).

`adk web` used to build the app itself; this builds the same app with
`get_fast_api_app` and adds three routes on top:

- `GET  /approvals`            pending approvals and questions of a session
- `POST /approvals/{id}/reply` answer one (once / always / reject, or a question's answer)
- `GET  /approvals/stream`     the same list as server-sent events

Nothing is stored here. The routes read the session and resume it through
ADK's own REST routes (`GET /apps/.../sessions/...`, `POST /run`), called
in-process, so any client that answers gets the same result.
"""
import asyncio
import json
import os
import weakref
from typing import Literal, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from google.adk.cli.fast_api import get_fast_api_app
from pydantic import BaseModel

from . import approvals

# The directory holding `dak_agent/` (what `adk web` got as its working directory).
AGENTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STREAM_INTERVAL_SECONDS = 2.0

app = get_fast_api_app(
    agents_dir=AGENTS_DIR,
    session_service_uri=os.environ["SESSION_SERVICE_URI"],
    allow_origins=os.getenv("DAK_ALLOW_ORIGINS", "http://localhost:3000").split(","),
    web=True,
    a2a=True,
    host="0.0.0.0",
    port=8000,
)

router = APIRouter(prefix="/approvals")

# One reply at a time per session: two replies to the same id must not both
# pass the pending check (ADK itself would run the tool twice). Held only by
# a running reply, so an idle session's lock is collected.
_session_locks: "weakref.WeakValueDictionary[tuple, asyncio.Lock]" = weakref.WeakValueDictionary()


def _adk() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://adk", timeout=None)


async def _session_events(client: httpx.AsyncClient, app_name: str, user_id: str, session_id: str) -> list[dict]:
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
            message = approvals.build_reply_function_response(
                approval_id, "timed_out" if timed_out else body.mode, "" if timed_out else body.reason)

        resp = await client.post("/run", json={
            "app_name": body.app_name, "user_id": body.user_id,
            "session_id": body.session_id, "new_message": message,
        })
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail=resp.text)
        if timed_out:
            return JSONResponse(status_code=409, content={"observation": "timed_out"})
        return resp.json()


@router.get("/stream")
async def stream(request: Request, user_id: str, session_id: str, app_name: str = "dak_agent"):
    """`approval.asked` for each new pending item, `approval.replied` when one
    leaves the list. Polls every STREAM_INTERVAL_SECONDS until the client goes."""
    async with _adk() as client:  # an unknown session is a 404, not a stream that breaks
        await _session_events(client, app_name, user_id, session_id)

    async def events():
        seen: dict[str, dict] = {}
        async with _adk() as client:
            while not await request.is_disconnected():
                current = {p["id"]: p for p in await _pending(client, app_name, user_id, session_id)}
                for pid, item in current.items():
                    if pid not in seen:
                        yield f"event: approval.asked\ndata: {json.dumps(item)}\n\n"
                for pid in seen.keys() - current.keys():
                    yield f"event: approval.replied\ndata: {json.dumps({'id': pid})}\n\n"
                seen = current
                await asyncio.sleep(STREAM_INTERVAL_SECONDS)

    return StreamingResponse(events(), media_type="text/event-stream")


app.include_router(router)
