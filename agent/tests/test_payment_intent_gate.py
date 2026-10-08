"""send_sol_payment is gated by the signed payment intent (AdaptiveAgent._before_tool).

Keys are generated at test time (helpers from test_payment_intent); the wallet
is the mock one, so a blocked call must leave its balance unchanged.
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from google.adk.tools import FunctionTool

from dak_agent.adaptive_agent import (
    PAYMENT_INTENT_CHECK_ENV,
    PAYMENT_INTENT_TRUSTED_JWKS_ENV,
    STATE_PAYMENT_INTENT,
    AdaptiveAgent,
)
from dak_agent.permission import DEFAULT_RULES, Rule
from skills.solana_wallet import tools as wallet_tools
from test_payment_intent import OTHER, PAYEE, jwk, present
from test_payment_intent import issue as issue_at
from test_permission import Harness, ScriptedLlm, _state_event, responses

SEND = SimpleNamespace(name="send_sol_payment")


def issue(key):
    # The gate checks against the real clock, so the intent is valid from now.
    now = datetime.now(timezone.utc)
    return issue_at(key, issued_at=now - timedelta(minutes=1), expires_at=now + timedelta(days=1))


@pytest.fixture
def key():
    return ec.generate_private_key(ec.SECP256R1())


@pytest.fixture
def wallet(monkeypatch):
    monkeypatch.setenv("SOLANA_USE_MOCK", "true")
    monkeypatch.setattr(wallet_tools, "_wallet_manager", None)
    return wallet_tools._get_wallet()


def make_agent(monkeypatch, enabled=True, jwks=None, model="test-model", tools=()):
    monkeypatch.setenv(PAYMENT_INTENT_CHECK_ENV, "true" if enabled else "false")
    if jwks is None:
        monkeypatch.delenv(PAYMENT_INTENT_TRUSTED_JWKS_ENV, raising=False)
    else:
        monkeypatch.setenv(PAYMENT_INTENT_TRUSTED_JWKS_ENV, jwks)
    with patch("dak_agent.adaptive_agent.ModeManager"), patch("dak_agent.adaptive_agent.SkillRegistry"):
        return AdaptiveAgent(model=model, name="dak_agent", instruction="test", tools=list(tools))


def context(intent=None):
    return SimpleNamespace(state={} if intent is None else {STATE_PAYMENT_INTENT: intent})


def pay(agent, wallet, ctx, recipient=PAYEE, amount=0.5):
    """What ADK does: run the before-tool callback, then the tool only if it returned None."""
    args = {"recipient": recipient, "amount": amount}
    blocked = agent._before_tool(SEND, args, ctx)
    if blocked is not None:
        return blocked
    return wallet_tools.send_sol_payment(**args)


def assert_blocked(result, wallet, reason):
    assert isinstance(result, dict)
    assert result["error"].startswith("Payment blocked by payment intent:")
    assert reason in result["error"]
    assert wallet.get_balance() == 1000.0


def test_disabled_check_lets_payment_through(monkeypatch, wallet):
    agent = make_agent(monkeypatch, enabled=False)
    result = pay(agent, wallet, context())
    assert "SOL Payment Sent" in result
    assert wallet.get_balance() == 999.5


def test_missing_intent_is_blocked(monkeypatch, wallet, key):
    agent = make_agent(monkeypatch, jwks=json.dumps({"keys": [jwk(key)]}))
    assert_blocked(pay(agent, wallet, context()), wallet, "no payment intent")


def test_out_of_range_amount_is_blocked(monkeypatch, wallet, key):
    agent = make_agent(monkeypatch, jwks=json.dumps({"keys": [jwk(key)]}))
    result = pay(agent, wallet, context(present(*issue(key))), amount=1.5)
    assert_blocked(result, wallet, "(constraint: payment.amount)")


def test_payee_outside_allowlist_is_blocked(monkeypatch, wallet, key):
    agent = make_agent(monkeypatch, jwks=json.dumps({"keys": [jwk(key)]}))
    result = pay(agent, wallet, context(present(*issue(key))), recipient=OTHER)
    assert_blocked(result, wallet, "(constraint: payment.allowed_payee)")


def test_within_range_pays_as_before(monkeypatch, wallet, key):
    agent = make_agent(monkeypatch, jwks=json.dumps({"keys": [jwk(key)]}))
    result = pay(agent, wallet, context(present(*issue(key))))
    assert "SOL Payment Sent" in result
    assert wallet.get_balance() == 999.5


@pytest.mark.parametrize("jwks", [
    None, "not json", "[" * 100_000, '{"keys": []}', '{"keys": ["not a key"]}',
    '{"keys": [{"kty": "EC", "crv": "P-256", "x": null, "y": null}]}',
], ids=["unset", "not-json", "deep-nesting", "no-keys", "key-not-object", "null-coordinates"])
def test_enabled_without_usable_keys_blocks_every_payment(monkeypatch, wallet, key, jwks):
    agent = make_agent(monkeypatch, jwks=jwks)
    assert_blocked(pay(agent, wallet, context(present(*issue(key)))), wallet, PAYMENT_INTENT_TRUSTED_JWKS_ENV)


@pytest.mark.parametrize("amount", ["abc", None, 10**400, float("nan")])
def test_invalid_amount_is_blocked(monkeypatch, wallet, key, amount):
    agent = make_agent(monkeypatch, jwks=json.dumps({"keys": [jwk(key)]}))
    result = agent._before_tool(SEND, {"recipient": PAYEE, "amount": amount}, context(present(*issue(key))))
    assert_blocked(result, wallet, "invalid amount")


def test_other_tools_are_not_checked(monkeypatch, key):
    agent = make_agent(monkeypatch, jwks=json.dumps({"keys": [jwk(key)]}))
    assert agent._before_tool(SimpleNamespace(name="check_solana_balance"), {}, context()) is None


def test_agent_registers_the_gate_as_before_tool_callback(monkeypatch):
    agent = make_agent(monkeypatch, enabled=False)
    assert agent.before_tool_callback == agent._before_tool


@pytest.mark.parametrize("action, observation", [
    ("deny", "denied_by_policy"),  # the permission plugin answers first; the intent check never runs
    ("allow", "Payment blocked by payment intent"),  # allowed by the rules, then blocked by the intent
])
def test_permission_plugin_runs_before_the_intent_check(monkeypatch, wallet, key, action, observation):
    llm = ScriptedLlm(model="scripted", call={"name": "send_sol_payment", "args": {"recipient": OTHER, "amount": 0.5}},
                      requests=[])
    agent = make_agent(monkeypatch, jwks=json.dumps({"keys": [jwk(key)]}), model=llm,
                       tools=[FunctionTool(wallet_tools.send_sol_payment, require_confirmation=False)])
    h = Harness(None, {}, rules=DEFAULT_RULES + [Rule("local", "send_sol_payment", "*", action)], agent=agent)
    asyncio.run(h.sessions.append_event(h.session, _state_event({STATE_PAYMENT_INTENT: present(*issue(key))})))
    h.session = asyncio.run(h.sessions.get_session(app_name="dak_agent", user_id="u", session_id=h.session.id))

    with patch("dak_agent.remote_tools.discover_remote_tools", return_value={}):
        events = h.say("pay")

    assert observation in json.dumps(responses(events, "send_sol_payment")[0])
    assert wallet.get_balance() == 1000.0
