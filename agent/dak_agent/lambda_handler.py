"""Lambda entry point: one turn per invocation, no HTTP server and no database.

Event: {"message": "<text>", "state_delta": {"dak:...": ...}}
Result: {"status": "succeeded", "output": "<final text>"}
     or {"status": "failed", "error": "<class name | invalid_event>"}

Each call runs in a throwaway InMemory session, so nothing is written to disk.
Neither the message nor the state_delta values are logged or returned in errors.
Only the standard library is imported at module level: `dak_agent.agent` (telemetry,
Langfuse auth check, wallets) loads on the first call, not in Lambda's 10 s Init.
"""
import asyncio
import logging
import uuid

logger = logging.getLogger(__name__)

_USER_ID = "lambda"
_app_cache = None


def _app():
    global _app_cache
    if _app_cache is None:
        from .agent import app
        _app_cache = app
    return _app_cache


async def _run_turn(message: str, state_delta: dict) -> str:
    from google.adk.artifacts import InMemoryArtifactService
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    app = _app()
    sessions = InMemorySessionService()
    session_id = str(uuid.uuid4())
    await sessions.create_session(app_name=app.name, user_id=_USER_ID, session_id=session_id)
    runner = Runner(app=app, session_service=sessions, artifact_service=InMemoryArtifactService())

    # No MCP server is deployed with the Lambda; do not wait for one that is unreachable.
    state_delta = {"dak:tools": [], **state_delta}
    output = ""
    async for event in runner.run_async(
        user_id=_USER_ID,
        session_id=session_id,
        new_message=types.Content(role="user", parts=[types.Part(text=message)]),
        state_delta=state_delta,
    ):
        if event.is_final_response() and event.content and event.content.parts:
            text = "".join(p.text for p in event.content.parts if p.text)
            if text:
                output = text
    return output


async def run(event) -> dict:
    message = event.get("message") if isinstance(event, dict) else None
    state_delta = event.get("state_delta", {}) if isinstance(event, dict) else None
    if not isinstance(message, str) or not isinstance(state_delta, dict):
        return {"status": "failed", "error": "invalid_event"}
    try:
        return {"status": "succeeded", "output": await _run_turn(message, state_delta)}
    except Exception as exc:  # the message may contain the request text: log the class only
        logger.error("turn failed: %s", type(exc).__name__)
        return {"status": "failed", "error": type(exc).__name__}


def handler(event, context=None):
    return asyncio.run(run(event))
