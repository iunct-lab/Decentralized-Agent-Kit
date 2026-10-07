"""`dak-cli acp`: DAK as an Agent Client Protocol agent over stdin/stdout.

The editor starts this process and talks JSON-RPC to it; each ACP session is a
DAK session, each prompt one turn of ADK's /run_sse, and its events become
`session/update`s. The mapping is docs/architecture/acp_adapter.md (§3).
stdout carries the protocol: log to stderr only, never print.
"""
import asyncio
import json
import logging
from typing import Any, Dict, List

from acp import Agent, RequestError, start_tool_call, text_block, tool_content, update_agent_message_text, \
    update_agent_thought_text, update_tool_call
from acp.schema import AgentCapabilities, Implementation, InitializeResponse, NewSessionResponse, \
    PromptCapabilities, PromptResponse

from .client import AgentClient

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
CONFIRMATION = "adk_request_confirmation"
RESULT_PREVIEW_CHARS = 2000


def _unwrapped(response: Any) -> Any:
    """A hook's rewrite (harness.py: hook_rewrote_input / _output) wraps the tool's own result."""
    while isinstance(response, dict) and str(response.get("observation", "")).startswith("hook_rewrote_"):
        response = response.get("result")
    return response


def _result_text(response: Any) -> str:
    """The start of a tool's result as text: an MCP tool's content, a built-in tool's `result`."""
    response = _unwrapped(response)
    if isinstance(response, dict) and isinstance(response.get("content"), list):
        text = "\n".join(c.get("text", "") for c in response["content"] if isinstance(c, dict))
    elif isinstance(response, dict) and isinstance(response.get("result"), str):
        text = response["result"]
    else:
        text = json.dumps(response, ensure_ascii=False)
    return text[:RESULT_PREVIEW_CHARS]


def _failed(response: Any) -> bool:
    """Not run or failed: an error, MCP's isError, or an observation (denied_by_policy, unknown_tool, ...)."""
    response = _unwrapped(response)
    return isinstance(response, dict) and (
        "error" in response or "observation" in response or response.get("isError") is True)


def events_to_updates(event: Dict[str, Any]) -> List[Any]:
    """One ADK event -> the session/update payloads it stands for (acp_adapter.md §3)."""
    waiting = (event.get("actions") or {}).get("requestedToolConfirmations") or {}
    updates = []
    for part in (event.get("content") or {}).get("parts", []):
        if "text" in part and not event.get("partial"):
            make = update_agent_thought_text if part.get("thought") else update_agent_message_text
            updates.append(make(part["text"]))
        elif "functionCall" in part:
            call = part["functionCall"]
            if call.get("name") != CONFIRMATION:
                updates.append(start_tool_call(call["id"], call["name"], kind="other", status="in_progress",
                                               raw_input=call.get("args")))
        elif "functionResponse" in part:
            result = part["functionResponse"]
            response = result.get("response")
            if result["id"] in waiting:  # "requires confirmation": not a failure, the call waits for an answer
                updates.append(update_tool_call(result["id"], status="pending"))
            else:
                updates.append(update_tool_call(
                    result["id"], status="failed" if _failed(response) else "completed",
                    content=[tool_content(text_block(_result_text(response)))], raw_output=response))
    if event.get("errorCode") or event.get("errorMessage"):  # the model answered with an error
        updates.append(update_agent_message_text(f"[error {event.get('errorCode', '')}] {event.get('errorMessage', '')}"))
    return updates


def _confirmations(event: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [p["functionCall"] for p in (event.get("content") or {}).get("parts", [])
            if p.get("functionCall", {}).get("name") == CONFIRMATION]


def _prompt_text(prompt: List[Any]) -> str:
    """Text blocks joined; a resource link (every agent must take one) as its URI on its own line."""
    text, links = "", []
    for block in prompt:
        if block.type == "text":
            text += block.text
        elif block.type == "resource_link":
            links.append(block.uri)
    return "\n".join([text, *links])


class DakAcpAgent(Agent):
    def __init__(self):
        self._conn = None
        self._clients: Dict[str, AgentClient] = {}

    def on_connect(self, conn) -> None:
        self._conn = conn

    async def initialize(self, protocol_version: int, client_capabilities=None, client_info=None, **kwargs):
        return InitializeResponse(
            protocol_version=PROTOCOL_VERSION,
            agent_capabilities=AgentCapabilities(load_session=False, prompt_capabilities=PromptCapabilities(
                image=False, audio=False, embedded_context=False)),
            auth_methods=[],
            agent_info=Implementation(name="dak-cli", version="0.1.0"),
        )

    async def new_session(self, cwd: str, mcp_servers=None, **kwargs):
        client = AgentClient()
        if not client.username:
            raise RequestError.auth_required({"message": "Not logged in. Run 'dak-cli login' first."})
        await asyncio.to_thread(client._ensure_session)
        logger.info("session %s: the editor's cwd (%s) and %d MCP server(s) are not used "
                    "(docs/architecture/acp_adapter.md §4)", client.session_id, cwd, len(mcp_servers or []))
        self._clients[client.session_id] = client
        return NewSessionResponse(session_id=client.session_id)

    async def prompt(self, prompt, session_id: str, **kwargs):
        client = self._clients.get(session_id)
        if client is None:
            raise RequestError.invalid_params({"message": f"unknown session {session_id}"})
        loop = asyncio.get_running_loop()
        events: asyncio.Queue = asyncio.Queue()

        def pump():  # requests blocks; read the stream in a thread and hand each event over
            try:
                for event in client.stream_events({"parts": [{"text": _prompt_text(prompt)}]}):
                    loop.call_soon_threadsafe(events.put_nowait, event)
            finally:
                loop.call_soon_threadsafe(events.put_nowait, None)

        reading = loop.run_in_executor(None, pump)
        asked, failure = [], None
        while (event := await events.get()) is not None:
            if "error" in event and "author" not in event:  # the runner raised; ADK's last data: line says what
                failure = event["error"]
                continue
            asked += _confirmations(event)
            for update in events_to_updates(event):
                await self._conn.session_update(session_id=session_id, update=update)
        await reading  # raises what the stream raised
        if failure:
            raise RequestError.internal_error({"message": failure})
        for call in asked:  # answering comes with #317; until then say why the tool did not run
            name = call.get("args", {}).get("originalFunctionCall", {}).get("name")
            await self._conn.session_update(session_id=session_id, update=update_agent_message_text(
                f"{name} needs approval, which dak-cli acp cannot give yet; it was not run from here "
                "and still waits (the BFF or `dak-cli approve` can answer it)."))
        return PromptResponse(stop_reason="end_turn")
