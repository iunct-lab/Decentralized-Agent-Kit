"""Tests for the payment intent verifier (dak_agent.payment_intent).

Every key is generated at test time; no key material is written to a file.
The SD-JWTs are issued here with the same rules the verifier checks
(docs/design/verifiable_intent.md, "DAK で検証する範囲").
"""
import base64
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from dak_agent.payment_intent import IntentDecision, sol_to_lamports, verify_payment_intent

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
PAYEE = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
OTHER = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def digest(disclosure: str) -> str:
    return b64u(hashlib.sha256(disclosure.encode()).digest())


def disclose(*parts) -> str:
    return b64u(json.dumps([b64u(os.urandom(16)), *parts]).encode())


def jwk(key: ec.EllipticCurvePrivateKey, kid: str = "user-1") -> dict:
    numbers = key.public_key().public_numbers()
    return {
        "kty": "EC",
        "crv": "P-256",
        "kid": kid,
        "x": b64u(numbers.x.to_bytes(32, "big")),
        "y": b64u(numbers.y.to_bytes(32, "big")),
    }


def sign(key: ec.EllipticCurvePrivateKey, header: dict, payload: dict) -> str:
    signing_input = f"{b64u(json.dumps(header).encode())}.{b64u(json.dumps(payload).encode())}"
    r, s = decode_dss_signature(key.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256())))
    return f"{signing_input}.{b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"


def amount(currency="SOL", min=None, max=1_000_000_000) -> dict:
    c = {"type": "payment.amount", "currency": currency}
    if min is not None:
        c["min"] = min
    if max is not None:
        c["max"] = max
    return c


def issue(key, constraints=None, payees=(PAYEE,), *, alg="ES256", issued_at=None, expires_at=None, extra_mandates=(), **claims):
    """Issue an L2 SD-JWT; returns (sd_jwt, disclosures) with every disclosure attached.

    `payees` are added to a `payment.allowed_payee` constraint as nested
    disclosures (`{"...": hash}`) unless `constraints` is given. `claims`
    override the payload's claims.
    """
    disclosures = []
    if constraints is None:
        payee_refs = []
        for p in payees:
            d = disclose({"id": p, "name": "shop"})
            disclosures.append(d)
            payee_refs.append({"...": digest(d)})
        constraints = [amount(), {"type": "payment.allowed_payee", "allowed_payees": payee_refs}]
    refs = []
    for mandate in ({"vct": "mandate.payment.open", "constraints": constraints}, *extra_mandates):
        d = disclose(mandate)
        disclosures.append(d)
        refs.append({"...": digest(d)})
    payload = {
        "nonce": "n-1",
        "aud": "dak",
        "iat": int((issued_at or NOW - timedelta(minutes=1)).timestamp()),
        "exp": int((expires_at or NOW + timedelta(days=1)).timestamp()),
        "_sd_alg": "sha-256",
        "delegate_payload": refs,
        **claims,
    }
    jws = sign(key, {"alg": alg, "typ": "kb-sd-jwt+kb", "kid": "user-1"}, payload)
    return jws, disclosures


def present(jws: str, disclosures) -> str:
    return "~".join([jws, *disclosures]) + "~"


@pytest.fixture
def key():
    return ec.generate_private_key(ec.SECP256R1())


@pytest.fixture
def jwks(key):
    return {"keys": [jwk(key)]}


def check(sd_jwt, jwks, payee=PAYEE, amount_minor=500_000_000, currency="SOL", now=NOW) -> IntentDecision:
    return verify_payment_intent(sd_jwt, jwks, payee=payee, amount_minor=amount_minor, currency=currency, now=now)


def assert_rejected(decision: IntentDecision, reason: str, constraint=None):
    assert decision.allowed is False
    assert reason in decision.reason
    assert decision.constraint == constraint


def test_within_range_is_allowed(key, jwks):
    decision = check(present(*issue(key)), jwks)
    assert decision.allowed is True
    assert decision.constraint is None


def test_signature_by_untrusted_key_is_rejected(key):
    other = {"keys": [jwk(ec.generate_private_key(ec.SECP256R1()))]}
    assert_rejected(check(present(*issue(key)), other), "signature")


def test_non_es256_alg_is_rejected(key, jwks):
    assert_rejected(check(present(*issue(key, alg="ES384")), jwks), "alg")


def test_tampered_mandate_disclosure_is_rejected(key, jwks):
    jws, disclosures = issue(key)
    forged = disclose({"vct": "mandate.payment.open", "constraints": [amount(max=10**15)]})
    assert_rejected(check(present(jws, [*disclosures[:-1], forged]), jwks), "digest")


def test_swapped_nested_payee_is_rejected(key, jwks):
    jws, disclosures = issue(key)
    swapped = disclose({"id": OTHER, "name": "shop"})
    decision = check(present(jws, [swapped, *disclosures[1:]]), jwks, payee=OTHER)
    assert_rejected(decision, "digest")


def test_hidden_disclosure_is_rejected(key, jwks):
    jws, disclosures = issue(key)
    # Withhold the payee item: the allowlist would otherwise look empty / skipped.
    assert_rejected(check(present(jws, disclosures[1:]), jwks), "digest")


def test_unreferenced_disclosure_is_rejected(key, jwks):
    jws, disclosures = issue(key)
    stray = disclose({"id": OTHER})
    assert_rejected(check(present(jws, [*disclosures, stray]), jwks), "unused disclosure")


def test_expired_intent_is_rejected(key, jwks):
    sd_jwt = present(*issue(key, issued_at=NOW - timedelta(days=2), expires_at=NOW - timedelta(seconds=301)))
    assert_rejected(check(sd_jwt, jwks), "expired")


def test_expiry_within_clock_skew_is_allowed(key, jwks):
    sd_jwt = present(*issue(key, issued_at=NOW - timedelta(days=2), expires_at=NOW - timedelta(seconds=299)))
    assert check(sd_jwt, jwks).allowed is True


def test_issued_in_the_future_is_rejected(key, jwks):
    assert_rejected(check(present(*issue(key, issued_at=NOW + timedelta(seconds=301))), jwks), "iat")


def test_amount_over_max_is_rejected(key, jwks):
    assert_rejected(check(present(*issue(key)), jwks, amount_minor=1_000_000_001), "above max", "payment.amount")


def test_amount_below_min_is_rejected(key, jwks):
    constraints = [amount(min=100, max=None)]
    assert_rejected(check(present(*issue(key, constraints)), jwks, amount_minor=99), "below min", "payment.amount")


def test_amount_without_max_is_unbounded_above(key, jwks):
    constraints = [amount(min=100, max=None)]
    assert check(present(*issue(key, constraints)), jwks, amount_minor=10**18).allowed is True


def test_currency_mismatch_is_rejected(key, jwks):
    constraints = [amount(currency="USD")]
    assert_rejected(check(present(*issue(key, constraints)), jwks), "currency", "payment.amount")


def test_payee_not_in_allowlist_is_rejected(key, jwks):
    assert_rejected(check(present(*issue(key)), jwks, payee=OTHER), "not in allowed_payees", "payment.allowed_payee")


def test_empty_allowlist_is_rejected(key, jwks):
    assert_rejected(check(present(*issue(key, payees=())), jwks), "empty", "payment.allowed_payee")


def test_unknown_constraint_is_rejected(key, jwks):
    constraints = [amount(), {"type": "payment.budget", "currency": "SOL", "max": 10}]
    assert_rejected(check(present(*issue(key, constraints)), jwks), "unsupported constraint", "payment.budget")


def test_second_mandate_is_rejected(key, jwks):
    checkout = {"vct": "mandate.checkout.open", "constraints": []}
    assert_rejected(check(present(*issue(key, extra_mandates=(checkout,))), jwks), "exactly one")


def test_malformed_input_is_rejected_without_raising(jwks):
    assert_rejected(check("not-a-jwt", jwks), "malformed")


@pytest.mark.parametrize(
    "header", ["[" * 100_000 + "]" * 100_000, '{"alg": ' + "1" * 5000 + "}"], ids=["deep-nesting", "huge-int"]
)
def test_unparsable_header_is_rejected_without_raising(jwks, header):
    sd_jwt = f"{b64u(header.encode())}.e30.AA~"
    assert_rejected(check(sd_jwt, jwks), "malformed")


@pytest.mark.parametrize("claim", [{"exp": float("nan")}, {"exp": float("inf")}, {"iat": float("nan")}])
def test_non_finite_time_is_rejected(key, jwks, claim):
    assert_rejected(check(present(*issue(key, **claim)), jwks), "non-finite")


def test_object_sd_disclosure_is_resolved(key, jwks):
    # The mandate's `constraints` is itself selectively disclosed through `_sd`.
    hidden = disclose("constraints", [amount(), {"type": "payment.allowed_payee", "allowed_payees": [{"id": PAYEE}]}])
    mandate = disclose({"vct": "mandate.payment.open", "_sd": [digest(hidden)]})
    jws, _ = issue(key, delegate_payload=[{"...": digest(mandate)}])
    assert check(present(jws, [hidden, mandate]), jwks).allowed is True
    assert_rejected(check(present(jws, [mandate]), jwks), "digest")


def test_digest_referenced_twice_is_rejected(key, jwks):
    jws, disclosures = issue(key)
    ref = {"...": digest(disclosures[-1])}
    jws, _ = issue(key, delegate_payload=[ref, ref])
    assert_rejected(check(present(jws, disclosures), jwks), "more than once")


def test_disclosure_kind_mismatch_is_rejected(key, jwks):
    # An object-property disclosure ([salt, name, value]) used as an array element.
    named = disclose("mandate", {"vct": "mandate.payment.open", "constraints": [amount()]})
    jws, _ = issue(key, delegate_payload=[{"...": digest(named)}])
    assert_rejected(check(present(jws, [named]), jwks), "kind")


@pytest.mark.parametrize(
    "sol, lamports",
    [(0.1, 100_000_000), (0.29, 290_000_000), (1, 1_000_000_000), (0.0000000001, 1), (1.0000000001, 1_000_000_001)],
)
def test_sol_to_lamports_rounds_up_fractions(sol, lamports):
    assert sol_to_lamports(sol) == lamports


@pytest.mark.parametrize("sol", [float("nan"), float("inf"), -0.1])
def test_sol_to_lamports_rejects_invalid_amounts(sol):
    with pytest.raises(ValueError):
        sol_to_lamports(sol)
