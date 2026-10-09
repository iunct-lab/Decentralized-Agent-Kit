"""Payment intent check E2E against the agent-ap2-intent instance (mock wallet).

With ENABLE_PAYMENT_INTENT_CHECK=true, send_sol_payment is blocked before it
runs when the session has no signed intent, or one whose signature does not
match the trusted public key. The allowed case needs the trusted private key,
which exists nowhere; it is covered by agent/tests/test_payment_intent_gate.py.
"""
import base64
import hashlib
import json
import os

import pytest

from conftest import function_responses

MODEL = "fake-ap2-intent"
PAYEE = "MockSoLAddress1111111111111111111111111111111"
INTENT_KEY = "dak:payment_intent"


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unsigned_intent() -> str:
    """A well-formed L2 SD-JWT whose ES256 signature is random bytes."""
    mandate = b64u(json.dumps([b64u(os.urandom(16)), {
        "vct": "mandate.payment.open",
        "constraints": [{"type": "payment.amount", "currency": "SOL", "max": 100 * 10**9}],
    }]).encode())
    header = {"alg": "ES256", "typ": "kb-sd-jwt+kb", "kid": "it-user"}
    digest = b64u(hashlib.sha256(mandate.encode()).digest())
    payload = {"iat": 0, "exp": 2**40, "_sd_alg": "sha-256", "delegate_payload": [{"...": digest}]}
    jws = ".".join([b64u(json.dumps(header).encode()), b64u(json.dumps(payload).encode()), b64u(os.urandom(64))])
    return f"{jws}~{mandate}~"


def _payment_response(events) -> str:
    matches = [r for r in function_responses(events) if r.get("name") == "send_sol_payment"]
    assert matches, f"no functionResponse for send_sol_payment in {events}"
    return str(matches[-1].get("response", {}))


@pytest.mark.parametrize("state, reason", [
    (None, "no payment intent"),
    ({INTENT_KEY: unsigned_intent()}, "signature does not verify"),
], ids=["no-intent", "bad-signature"])
def test_payment_without_a_valid_intent_is_blocked(agent_ap2_intent, fake_llm, state, reason):
    fake_llm.clear(MODEL)
    session_id = agent_ap2_intent.create_session(state)
    fake_llm.script(MODEL, [
        fake_llm.tool_call("send_sol_payment", recipient=PAYEE, amount=1.0),
        fake_llm.text("The payment was blocked."),
    ])

    events = agent_ap2_intent.run(session_id, "Pay 1 SOL")

    response = _payment_response(events)
    assert "Payment blocked by payment intent" in response
    assert reason in response
    assert "MockTx_" not in response
