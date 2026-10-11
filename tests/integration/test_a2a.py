"""A2A protocol integration tests.

A2A is the peer-to-peer seam of the kit (`agent/dak_agent/a2a_peer_manager.py`),
but it had no test coverage, so a breaking `a2a-sdk` bump passed CI green while
making every peer handshake fail (PR #76). These tests pin the agent-card wire
format (A2A 1.0, with a 0.3 interface for older peers) and actual exchanges with
an a2a-sdk 1.x client and with a 0.3 peer. The version policy is in
docs/architecture/a2a.md.
"""
import asyncio
import base64
import json
import os
import subprocess
import uuid

import httpx
import pytest

from conftest import AGENT_AP2_URL, AGENT_ENFORCER_URL, AGENT_URL, APP_NAME, event_texts, system_instruction

MODEL = "fake-default"
AGENT_CARD_URL = f"{AGENT_URL}/a2a/{APP_NAME}/.well-known/agent-card.json"
A2A_RPC_URL = f"{AGENT_URL}/a2a/{APP_NAME}"
AGENT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "agent"))
# Made by the card-keygen service when the stack comes up and none is there (docker-compose.test.yml)
CARD_PUBLIC_KEY = os.path.join(os.path.dirname(__file__), ".keys", "card-signing.pub.pem")


@pytest.fixture
def agent_card():
    resp = httpx.get(AGENT_CARD_URL, timeout=30.0)
    resp.raise_for_status()
    return resp.json()


def test_agent_card_is_served(agent_card):
    """`a2a_peer_manager` resolves peers at this well-known path."""
    assert agent_card["name"] == APP_NAME
    assert agent_card["skills"], "card advertises no skills"


def test_agent_card_declares_a2a_1_0_jsonrpc_interface(agent_card):
    """A2A 1.0 clients pick the endpoint from `supportedInterfaces`; a 0.3
    interface at the same URL keeps older peers (a2a-sdk 0.3, older DAKs) able
    to read the card, which then also carries the 0.3 top-level fields."""
    interfaces = {(i["protocolBinding"], i["protocolVersion"]): i["url"]
                  for i in agent_card.get("supportedInterfaces", [])}
    assert interfaces.get(("JSONRPC", "1.0"), "").endswith(f"/a2a/{APP_NAME}"), agent_card
    assert interfaces.get(("JSONRPC", "0.3")) == interfaces[("JSONRPC", "1.0")]
    assert agent_card["capabilities"].get("streaming") is True
    assert agent_card["url"] == interfaces[("JSONRPC", "1.0")]  # what a 0.3 peer reads


# a2a-sdk's verifier needs PyJWT, which this environment does not have; the
# agent's environment has it, so the check runs there (as test_approvals.py runs dak-cli)
VERIFY_CARD = """
import json, sys
from a2a.client.card_resolver import parse_agent_card
from a2a.utils.signing import create_signature_verifier
public_pem = open(sys.argv[1], "rb").read()
create_signature_verifier(lambda kid, jku: public_pem, ["ES256"])(parse_agent_card(json.load(sys.stdin)))
print("verified")
"""


def test_agent_card_signature_verifies_with_the_test_key(agent_card):
    """`agent` has AGENT_CARD_SIGNING_KEY_FILE set: the card it serves carries one
    ES256 signature with the configured kid, and a2a-sdk's create_signature_verifier
    accepts it with the public key (docs/architecture/a2a.md §7)."""
    signatures = agent_card.get("signatures", [])
    assert len(signatures) == 1, agent_card
    protected = signatures[0]["protected"]
    header = json.loads(base64.urlsafe_b64decode(protected + "=" * (-len(protected) % 4)))
    assert header == {"kid": "test-1", "alg": "ES256", "typ": "JOSE"}

    result = subprocess.run(["uv", "run", "python", "-c", VERIFY_CARD, CARD_PUBLIC_KEY], cwd=AGENT_DIR,
                            input=json.dumps(agent_card), capture_output=True, text=True, timeout=300)
    assert result.returncode == 0 and result.stdout.strip() == "verified", result.stderr


def test_agent_card_without_a_key_has_no_signatures():
    """`agent-enforcer` has no signing key: its card is served unsigned, as before."""
    resp = httpx.get(f"{AGENT_ENFORCER_URL}/a2a/{APP_NAME}/.well-known/agent-card.json", timeout=30.0)
    resp.raise_for_status()
    assert not resp.json().get("signatures"), resp.json()



def test_card_skills_list_the_loaded_skills():
    """`agent-ap2` loads /app/skills and /app/provider_skills (AGENT_SKILLS_DIRS):
    its card lists each skill, the provider's premium_service included."""
    resp = httpx.get(f"{AGENT_AP2_URL}/a2a/{APP_NAME}/.well-known/agent-card.json", timeout=30.0)
    resp.raise_for_status()
    ids = {s["id"] for s in resp.json()["skills"]}
    assert {"premium_service", "filesystem"} <= ids, resp.json()["skills"]

@pytest.mark.parametrize("streaming", [False, True])
def test_a2a_sdk_client_round_trip(fake_llm, streaming):
    """The official a2a-sdk 1.x client resolves the card and gets the reply
    over A2A 1.0 JSON-RPC (SendMessage / SendStreamingMessage)."""
    from a2a.client import ClientConfig, create_client
    from a2a.types import a2a_pb2 as pb

    fake_llm.clear(MODEL)
    fake_llm.script(MODEL, [fake_llm.text("A2A 1.0 pong from DAK.")])

    async def exchange():
        async with httpx.AsyncClient(timeout=120.0) as http:
            client = await create_client(A2A_RPC_URL, client_config=ClientConfig(streaming=streaming, httpx_client=http))
            request = pb.SendMessageRequest(message=pb.Message(
                message_id=uuid.uuid4().hex, role=pb.ROLE_USER, parts=[pb.Part(text="ping")]))
            try:
                return [event async for event in client.send_message(request)]
            finally:
                await client.close()

    events = asyncio.run(exchange())
    texts = [part.text for event in events
             for artifact in ([event.artifact_update.artifact] if event.HasField("artifact_update")
                              else event.task.artifacts if event.HasField("task") else [])
             for part in artifact.parts]
    assert "A2A 1.0 pong from DAK." in texts, events
    # The client falls back to SendMessage when the card does not declare streaming.
    assert any(event.HasField("status_update") for event in events) is streaming, events


def test_a2a_v0_3_message_send_round_trip(fake_llm):
    """A 0.3 peer (`message/send`) can still drive the agent and get its reply back."""
    fake_llm.clear(MODEL)
    fake_llm.script(MODEL, [fake_llm.text("A2A pong from DAK.")])

    payload = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                "parts": [{"kind": "text", "text": "ping"}],
                "messageId": uuid.uuid4().hex,
                "kind": "message",
            }
        },
    }
    resp = httpx.post(A2A_RPC_URL, json=payload, timeout=120.0)
    resp.raise_for_status()
    body = resp.json()

    assert "error" not in body, f"A2A call returned an error: {body}"
    result = body["result"]
    assert result["kind"] == "task"

    texts = [
        part.get("text")
        for artifact in result.get("artifacts") or []
        for part in artifact.get("parts") or []
    ]
    assert "A2A pong from DAK." in texts, f"agent reply missing from artifacts: {result}"


def test_delegation_to_peer_with_non_default_name(agent_consumer, fake_llm):
    """A DAK hands a task to another DAK over A2A and gets the answer back.
    The peer's card is named dak_peer; the consumer's a2a_peers names the
    peer's endpoint (tests/integration/fixtures/agent_config.consumer.yaml),
    and the card is looked up under it, not at a hardcoded /a2a/dak_agent."""
    fake_llm.clear("fake-consumer")
    fake_llm.clear("fake-peer")
    fake_llm.script("fake-consumer", [fake_llm.tool_call("transfer_to_agent", agent_name="dak_peer")])
    fake_llm.script("fake-peer", [fake_llm.text("Delegated answer from the peer DAK.")])

    session_id = agent_consumer.create_session()
    events = agent_consumer.run(session_id, "Ask the peer.")

    assert "Delegated answer from the peer DAK." in event_texts(events), events
    # The consumer checked agent-peer's card with the key in its a2a_peers, and the
    # result reached the model as the first line of the sub-agent's description
    assert "card signature: verified (kid=test-1)" in system_instruction(fake_llm.requests("fake-consumer")[-1])
