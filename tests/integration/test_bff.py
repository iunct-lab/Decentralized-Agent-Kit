"""BFF (HTMX UI) E2E: chat through the BFF reaches the agent and renders HTML."""
import os
import re
import uuid

import httpx
import pytest

from conftest import AGENT_RUN_TIMEOUT, BFF_URL

MODEL = "fake-default"


def test_index_serves_chat_page():
    resp = httpx.get(f"{BFF_URL}/", timeout=30.0)
    resp.raise_for_status()
    assert "session_bff_" in resp.text


def test_chat_round_trip(fake_llm):
    fake_llm.clear(MODEL)
    fake_llm.script(MODEL, [fake_llm.text("BFF round trip answer.")])

    session_id = f"session_bff_it_{uuid.uuid4().hex[:8]}"
    resp = httpx.post(
        f"{BFF_URL}/chat",
        data={
            "prompt": "Hello via BFF",
            "session_id": session_id,
            "user_id": f"user_{session_id}",
        },
        timeout=120.0,
    )
    resp.raise_for_status()

    assert "Hello via BFF" in resp.text  # user message echoed
    assert "BFF round trip answer." in resp.text  # agent answer rendered


# --- Approval card (PBI #21): a write_file on the default MCP server asks for
# confirmation (agent's permission rules); the BFF shows it and answers it
# through the agent's /approvals/{id}/reply. The mcp-server mounts the repo at
# /projects (its working directory), so the written file is seen here.

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
APPROVE_PATH = "tests/integration/artifacts/dak_bff_approve_test.txt"


def _ask_write(fake_llm, *after):
    """A /chat turn in which the model enables write_file and calls it; returns the response HTML."""
    fake_llm.clear(MODEL)
    fake_llm.script(MODEL, [
        fake_llm.tool_call("enable_skill", skill_name="write_file"),
        fake_llm.tool_call("write_file", path=APPROVE_PATH, content="hi"),
        fake_llm.text("Waiting for approval."),  # the call is held; the model gets one more step
        *after,
    ])
    session_id = f"session_bff_it_{uuid.uuid4().hex[:8]}"
    resp = httpx.post(
        f"{BFF_URL}/chat",
        data={"prompt": "Write hi into a file", "session_id": session_id, "user_id": f"user_{session_id}"},
        timeout=AGENT_RUN_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.text


def _answer(card_html: str, mode: str) -> httpx.Response:
    """Press Approve / Reject on the card: post its form the way HTMX does."""
    fc_id = re.search(r'/chat/approvals/([^"]+)"', card_html).group(1)
    fields = dict(re.findall(r'<input type="hidden" name="(session_id|user_id)" value="([^"]+)">', card_html))
    resp = httpx.post(f"{BFF_URL}/chat/approvals/{fc_id}", data={**fields, "mode": mode}, timeout=AGENT_RUN_TIMEOUT)
    resp.raise_for_status()
    return resp


@pytest.fixture
def written():
    path = os.path.join(REPO_ROOT, APPROVE_PATH)
    if os.path.exists(path):
        os.remove(path)
    yield path
    if os.path.exists(path):
        os.remove(path)


def test_chat_shows_approval_card_for_confirmation(fake_llm, written):
    html = _ask_write(fake_llm)

    assert "/chat/approvals/" in html
    assert "write_file" in html
    assert not os.path.exists(written)  # nothing runs before the answer


def test_chat_approve_executes_tool(fake_llm, written):
    html = _ask_write(fake_llm, fake_llm.text("Done, file written."))

    resp = _answer(html, "once")

    assert "Successfully wrote to" in resp.text
    assert "Done, file written." in resp.text
    assert os.path.exists(written)


def test_chat_reject_prevents_execution(fake_llm, written):
    html = _ask_write(fake_llm, fake_llm.text("Understood, not writing."))

    resp = _answer(html, "reject")

    assert "Successfully wrote to" not in resp.text
    assert "Understood, not writing." in resp.text
    assert not os.path.exists(written)
