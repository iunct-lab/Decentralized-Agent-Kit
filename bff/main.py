import html
import json
import os
import uuid
import httpx
import logging
from urllib.parse import quote
from fastapi import FastAPI, Request, Form
from fastapi.responses import StreamingResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# ADK Agent URL
AGENT_URL = os.getenv("AGENT_URL", "http://agent:8000")

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    # Generate a session ID for this page load
    session_id = f"session_bff_{uuid.uuid4()}"
    user_id = f"user_{session_id}"
    return templates.TemplateResponse("index.html", {"request": request, "session_id": session_id, "user_id": user_id})

@app.post("/chat")
async def chat(request: Request, prompt: str = Form(...), session_id: str = Form(...), user_id: str = Form(...)):
    
    async def event_generator():
        # 1. Send user message to UI immediately
        yield f'<div class="chat-message user"><div class="message-content">{prompt}</div></div>\n'
        
        # 2. Send loading indicator
        yield '<div id="loading-indicator" class="chat-message system">Thinking...</div>\n'

        # 2.5. Ensure Session Exists
        # Use a local variable for the session ID to use in calls, initialized from the argument
        current_session_id = session_id
        # Use the provided user ID
        current_user_id = user_id
        
        session_url = f"{AGENT_URL}/apps/dak_agent/users/{current_user_id}/sessions/{current_session_id}"
        headers = {
            "Content-Type": "application/json",
            "X-User-ID": current_user_id,
            "X-Session-ID": current_session_id
        }
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                # Check if session exists
                resp = await client.get(session_url, headers=headers)
                if resp.status_code != 200:
                    # Create session
                    create_url = f"{AGENT_URL}/apps/dak_agent/users/{current_user_id}/sessions"
                    create_resp = await client.post(create_url, json={"id": current_session_id}, headers=headers)
                    if create_resp.status_code == 200:
                         data = create_resp.json()
                         if "id" in data:
                             current_session_id = data["id"]
                             # Update headers with new session ID
                             headers["X-Session-ID"] = current_session_id
            except Exception as e:
                yield f'<div class="chat-message error">Session Error: {str(e)}</div>\n'
                return

        # 2.6. Update Client Session ID if changed
        if current_session_id != session_id:
            logger.info(f"Updating client session ID to: {current_session_id}")
            # Use HTMX OOB swap to update the hidden input field
            yield f'<input type="hidden" id="session-id-input" name="session_id" value="{current_session_id}" hx-swap-oob="true">\n'

        # 3. Call ADK Agent
        url = f"{AGENT_URL}/run"
        payload = {
            "app_name": "dak_agent",
            "user_id": current_user_id,
            "session_id": current_session_id,
            "new_message": {
                "parts": [{"text": prompt}]
            }
        }

        try:
            # Non-streaming fallback for now to ensure correctness
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(url, json=payload, headers=headers)
                response.raise_for_status()
                data = response.json()
                logger.info(f"Agent response type: {type(data)}")
                logger.info(f"Agent response content: {json.dumps(data)[:500]}...")
                
                # Remove loading indicator
                yield '<div id="loading-indicator" hx-swap-oob="true"></div>\n'
                
                # Normalize data to list
                events = data if isinstance(data, list) else [data]
                yield _render_agent_turn_html(events, current_session_id, current_user_id)

        except Exception as e:
            logger.error(f"Error in chat: {e}")
            yield f'<div class="chat-message error">Error: {str(e)}</div>\n'
            yield '<div id="loading-indicator" hx-swap-oob="true"></div>\n'

    return StreamingResponse(event_generator(), media_type="text/html")


# A tool call the agent holds for the user's answer (docs/design/approval-queue.md)
REQUEST_CONFIRMATION = "adk_request_confirmation"

# Replies the agent refused (docs/design/approval-queue.md): 409 still resumed
# the agent, so it must not read as "nothing happened"
APPROVAL_REPLY_NOTICES = {
    409: "This approval had expired: the agent was told it timed out and has moved on.",
    404: "This approval is no longer pending: it was already answered or dropped.",
}


def _render_agent_turn_html(events: list, session_id: str, user_id: str) -> str:
    """One agent turn (ADK events, as `/run` returns them) as chat HTML. Text
    and tool calls come from the model, so they are escaped: a script among
    them could press the Approve button of the card below."""
    thoughts = []
    response_text = ""
    approval_card = ""

    for event in events:
        if approval_card:
            break  # the turn waits for the answer; nothing after it is shown
        # Handle standard ADK event format
        if "content" in event and "parts" in event["content"]:
            for part in event["content"]["parts"]:
                # 1. Direct Text (Model thought or answer)
                if "text" in part:
                    response_text += html.escape(part["text"])

                # 2. Tool Calls (Thoughts/Actions)
                elif "functionCall" in part:
                    fc = part["functionCall"]
                    if fc.get("name") == REQUEST_CONFIRMATION:
                        approval_card = _render_approval_card(fc, session_id, user_id)
                        break
                    name = html.escape(str(fc.get("name", "unknown")))
                    args = html.escape(json.dumps(fc.get("args", {})))
                    thoughts.append(f'<div class="thought-item"><span class="thought-label">Action:</span> Called <strong>{name}</strong></div>')
                    thoughts.append(f'<div class="thought-args">{args}</div>')

                # 3. Tool Responses (Observations)
                elif "functionResponse" in part:
                    func_resp = part["functionResponse"]
                    name = html.escape(str(func_resp.get("name", "unknown")))

                    # Special handling for user-facing tools
                    if name in ["ask_question", "attempt_answer"]:
                        if "response" in func_resp and "result" in func_resp["response"]:
                            response_text += html.escape(str(func_resp["response"]["result"])) + "\n"
                    else:
                        # Internal tool results go to thoughts
                        result = "No result"
                        if "response" in func_resp:
                            result = json.dumps(func_resp["response"])

                        # Highlight Payment Errors
                        if "Payment Required" in result:
                            thoughts.append(f'<div class="thought-item error"><span class="thought-label">System:</span> <strong>Payment Required</strong></div>')

                        thoughts.append(f'<div class="thought-item"><span class="thought-label">Observation:</span> {name} returned: {html.escape(result[:200])}...</div>')

    # Construct HTML
    html_output = '<div class="chat-message assistant">'

    # Add Thoughts block if exists
    if thoughts:
        html_output += f'''
        <details class="thoughts">
            <summary>Thinking Process ({len(thoughts)} steps)</summary>
            <div class="thought-content">
                {"".join(thoughts)}
            </div>
        </details>
        '''

    # Add Final Answer
    html_output += f'<div class="message-content">{response_text}</div>'
    html_output += '</div>\n'
    return html_output + approval_card


def _render_approval_card(fc: dict, session_id: str, user_id: str) -> str:
    """Approve / Reject buttons for one held call. The values come from the
    model and the page, so they are escaped."""
    original = fc.get("args", {}).get("originalFunctionCall", {})
    fc_id = html.escape(quote(str(fc.get("id")), safe=""))
    tool_name = html.escape(str(original.get("name", "unknown")))
    tool_args = html.escape(json.dumps(original.get("args", {})))
    session_id, user_id = html.escape(session_id), html.escape(user_id)
    return (
        '<div class="chat-message system approval-card">'
        f'<div>Tool: <strong>{tool_name}</strong></div><div class="thought-args">{tool_args}</div>'
        f'<form hx-post="/chat/approvals/{fc_id}" hx-target="#chat-history" hx-swap="beforeend">'
        f'<input type="hidden" name="session_id" value="{session_id}">'
        f'<input type="hidden" name="user_id" value="{user_id}">'
        '<button type="submit" name="mode" value="once" class="approve">Approve</button>'
        '<button type="submit" name="mode" value="reject" class="reject">Reject</button>'
        '</form></div>\n'
    )


@app.post("/chat/approvals/{fc_id}")
async def answer_approval(fc_id: str, mode: str = Form(...), session_id: str = Form(...), user_id: str = Form(...)):
    """Answer a held call through the agent's /approvals (the route every client
    uses) and show the turn it resumes."""
    headers = {"Content-Type": "application/json", "X-User-ID": user_id, "X-Session-ID": session_id}
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{AGENT_URL}/approvals/{quote(fc_id, safe='')}/reply",
                json={"user_id": user_id, "session_id": session_id, "mode": mode},
                headers=headers,
            )
            if response.status_code in APPROVAL_REPLY_NOTICES:
                return HTMLResponse(f'<div class="chat-message system">{APPROVAL_REPLY_NOTICES[response.status_code]}</div>\n')
            response.raise_for_status()
            data = response.json()
    except Exception as e:
        logger.error(f"Error answering approval: {e}")
        return HTMLResponse(f'<div class="chat-message error">Error: {html.escape(str(e))}</div>\n')
    events = data if isinstance(data, list) else [data]
    return HTMLResponse(_render_agent_turn_html(events, session_id, user_id))
