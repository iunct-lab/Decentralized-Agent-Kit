"""Lambda entry point: one turn per invocation, no HTTP server and no database.

Event: {"message": "<text>", "state_delta": {"dak:...": ...}, "job_id": "<id>"?}
Result: {"status": "succeeded", "output": "<final text>"}
     or {"status": "failed", "error": "<class name | invalid_event | ...>"}

With a `job_id` the call is a job: its state and result are kept in the DynamoDB
table `DAK_TABLE` (pk `J#<job_id>`, sk `JOB`, expires after a day), so an async
(Event) caller can poll with {"op": "get", "job_id": "<id>"}. The status is one of
running | succeeded | failed | timed_out | not_found.

If `DAK_API_KEY_PARAMETER` names an SSM parameter, its value is read once into
`ANTHROPIC_API_KEY` (the key itself is in no file, image or log).

Each call runs in a throwaway InMemory session, so nothing is written to disk.
Neither the message nor the state_delta values are logged or returned in errors.
Only the standard library is imported at module level: `dak_agent.agent` (telemetry,
Langfuse auth check, wallets) loads on the first call, not in Lambda's 10 s Init.
"""
import asyncio
import logging
import os
import time
import uuid

logger = logging.getLogger(__name__)

_USER_ID = "lambda"
_app_cache = None
JOB_TTL_SECONDS = 24 * 3600
MAX_OUTPUT_BYTES = 350 * 1024  # DynamoDB's item limit is 400 KB
_loop = None  # one loop per container: the cached App may hold loop-bound async clients


def _ssm():
    """The SSM client. Tests replace this."""
    import boto3
    return boto3.client("ssm")


def _ensure_api_key() -> None:
    """Read the model API key from Parameter Store once per container.

    `DAK_API_KEY_PARAMETER` is the parameter's *name*; the value goes only into
    `ANTHROPIC_API_KEY` and is never logged or returned."""
    name = os.environ.get("DAK_API_KEY_PARAMETER")
    if not name or os.environ.get("ANTHROPIC_API_KEY"):
        return
    value = _ssm().get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]
    os.environ["ANTHROPIC_API_KEY"] = value


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


def _table():
    """The job table, or None when `DAK_TABLE` is not set. Tests replace this."""
    name = os.environ.get("DAK_TABLE")
    if not name:
        return None
    import boto3
    return boto3.resource("dynamodb").Table(name)


def _job_key(job_id: str) -> dict:
    return {"pk": f"J#{job_id}", "sk": "JOB"}


def _get_job(job_id) -> dict:
    if not isinstance(job_id, str) or not job_id:
        return {"status": "failed", "error": "invalid_event"}
    table = _table()
    if table is None:
        return {"status": "failed", "error": "table_not_configured"}
    item = table.get_item(Key=_job_key(job_id), ConsistentRead=True).get("Item")
    now = time.time()
    if not item or item.get("expires_at", now + 1) <= now:
        return {"job_id": job_id, "status": "not_found"}
    status = item["status"]
    if status == "running" and item.get("deadline", now + 1) < now:
        status = "timed_out"  # the Lambda was stopped by its timeout
    result = {"job_id": job_id, "status": status}
    for key in ("output", "error"):
        if key in item:
            result[key] = item[key]
    return result


def _start_job(table, job_id: str, context) -> bool:
    from botocore.exceptions import ClientError
    now = time.time()
    remaining_ms = context.get_remaining_time_in_millis() if context else 15 * 60 * 1000
    try:
        table.put_item(
            Item={**_job_key(job_id), "status": "running", "started_at": int(now),
                  "deadline": int(now + remaining_ms / 1000), "expires_at": int(now + JOB_TTL_SECONDS)},
            ConditionExpression="attribute_not_exists(pk)",
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise
    return True


def _finish_job(table, job_id: str, result: dict) -> None:
    fields = {"status": result["status"]}
    if "output" in result:
        if len(result["output"].encode()) > MAX_OUTPUT_BYTES:
            fields = {"status": "failed", "error": "output_too_large"}
        else:
            fields["output"] = result["output"]
    if "error" in result:
        fields["error"] = result["error"]
    table.update_item(
        Key=_job_key(job_id),
        UpdateExpression="SET " + ", ".join(f"#{k} = :{k}" for k in fields),
        ExpressionAttributeNames={f"#{k}": k for k in fields},
        ExpressionAttributeValues={f":{k}": v for k, v in fields.items()},
    )


async def run(event, context=None) -> dict:
    if isinstance(event, dict) and event.get("op") == "get":
        return _get_job(event.get("job_id"))
    job_id = event.get("job_id") if isinstance(event, dict) else None
    if job_id is not None and (not isinstance(job_id, str) or not job_id):
        return {"status": "failed", "error": "invalid_event"}
    if job_id is None:
        return await _turn(event)
    table = _table()
    if table is None:
        return {"status": "failed", "error": "table_not_configured"}
    if not isinstance(event.get("message"), str):
        return {"status": "failed", "error": "invalid_event"}
    try:
        started = _start_job(table, job_id, context)
    except Exception as exc:
        logger.error("job start failed: %s", type(exc).__name__)
        return {"status": "failed", "error": type(exc).__name__}
    if not started:
        return {"status": "failed", "error": "job_exists"}
    result = await _turn(event)
    try:
        _finish_job(table, job_id, result)
    except Exception as exc:
        logger.error("job update failed: %s", type(exc).__name__)
        return {"status": "failed", "error": type(exc).__name__}
    return result


async def _turn(event) -> dict:
    message = event.get("message") if isinstance(event, dict) else None
    state_delta = event.get("state_delta", {}) if isinstance(event, dict) else None
    if not isinstance(message, str) or not isinstance(state_delta, dict):
        return {"status": "failed", "error": "invalid_event"}
    from .permission import owned_state_keys  # same refusal as server.py: no pre-approval via state_delta
    if owned_state_keys(state_delta):
        return {"status": "failed", "error": "invalid_event"}
    try:
        _ensure_api_key()
        return {"status": "succeeded", "output": await _run_turn(message, state_delta)}
    except Exception as exc:  # the message may contain the request text: log the class only
        logger.error("turn failed: %s", type(exc).__name__)
        return {"status": "failed", "error": type(exc).__name__}


def handler(event, context=None):
    global _loop
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
    return _loop.run_until_complete(run(event, context))
