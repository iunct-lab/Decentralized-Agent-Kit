"""Verify a user-signed payment intent (SD-JWT) against one transfer.

The intent is the L2 credential of Verifiable Intent v0.1 in autonomous mode,
narrowed to DAK's profile (docs/design/verifiable_intent.md, "DAK で検証する範囲"):

- signed with ES256 by a key in the trusted JWKS (the header `alg` must be
  ES256; it never selects the algorithm)
- `_sd_alg` is sha-256 and every digest, nested ones included, resolves to a
  disclosure; every disclosure is used exactly once (no decoys, nothing hidden)
- `delegate_payload` holds exactly one mandate, `vct: mandate.payment.open`
- `exp` / `iat` are checked with a 300 second clock skew
- only `payment.amount` and `payment.allowed_payee` are enforced; any other
  constraint type rejects the intent

This module only decides. It never starts or approves a payment.
"""
import base64
import binascii
import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_CEILING, Decimal
from typing import Any, Dict, List, Optional, Tuple

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

CLOCK_SKEW_SECONDS = 300
LAMPORTS_PER_SOL = 10**9

PAYMENT_MANDATE_VCT = "mandate.payment.open"
AMOUNT = "payment.amount"
ALLOWED_PAYEE = "payment.allowed_payee"


@dataclass(frozen=True)
class IntentDecision:
    allowed: bool
    reason: str
    # The constraint type that rejected the transfer; None when allowed or when
    # the intent itself is invalid (signature, disclosures, time).
    constraint: Optional[str] = None


class _Invalid(Exception):
    """Internal: the intent is rejected for `reason`."""


def _reject(reason: str, constraint: Optional[str] = None) -> IntentDecision:
    return IntentDecision(allowed=False, reason=reason, constraint=constraint)


def sol_to_lamports(amount: float) -> int:
    """Convert a SOL amount to lamports, rounding any fraction of a lamport UP.

    The float is read through its shortest decimal repr (0.29 -> 290000000, not
    289999999), and anything below one lamport rounds up so the amount checked
    against the intent is never smaller than the amount the wallet sends.
    Raises ValueError for NaN, infinity or a negative amount.
    """
    if not math.isfinite(amount) or amount < 0:
        raise ValueError(f"invalid SOL amount: {amount!r}")
    lamports = Decimal(repr(float(amount))) * LAMPORTS_PER_SOL
    return int(lamports.to_integral_value(rounding=ROUND_CEILING))


def verify_payment_intent(
    sd_jwt: str,
    trusted_jwks: dict,
    *,
    payee: str,
    amount_minor: int,
    currency: str,
    now: datetime,
) -> IntentDecision:
    """Decide whether the intent allows paying `amount_minor` of `currency` to `payee`.

    Never raises for a bad intent; the reason is in the returned decision.
    """
    try:
        mandate = _verified_mandate(sd_jwt, trusted_jwks, now)
        return _check_constraints(mandate, payee=payee, amount_minor=amount_minor, currency=currency)
    except _Invalid as e:
        return _reject(str(e))


def _b64decode(part: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))
    except (binascii.Error, ValueError) as e:
        raise _Invalid("malformed SD-JWT: bad base64url") from e


def _json(part: str) -> Any:
    try:
        return json.loads(_b64decode(part))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise _Invalid("malformed SD-JWT: bad JSON") from e


def _verified_mandate(sd_jwt: str, trusted_jwks: dict, now: datetime) -> dict:
    parts = sd_jwt.split("~")
    if len(parts) < 2 or parts[-1] != "":
        raise _Invalid("malformed SD-JWT: expected '<JWS>~<disclosure>~...~'")
    jws, disclosures = parts[0], parts[1:-1]
    segments = jws.split(".")
    if len(segments) != 3:
        raise _Invalid("malformed SD-JWT: JWS must have 3 segments")

    header = _json(segments[0])
    if not isinstance(header, dict) or header.get("alg") != "ES256":
        raise _Invalid(f"unsupported alg {header.get('alg') if isinstance(header, dict) else None!r}: only ES256")
    _verify_signature(f"{segments[0]}.{segments[1]}".encode(), _b64decode(segments[2]), header, trusted_jwks)

    payload = _json(segments[1])
    if not isinstance(payload, dict):
        raise _Invalid("malformed SD-JWT: payload is not an object")
    if payload.get("_sd_alg") != "sha-256":
        raise _Invalid(f"unsupported _sd_alg {payload.get('_sd_alg')!r}: only sha-256")
    _check_time(payload, now)

    used: set = set()
    resolved = _resolve(payload, _index(disclosures), used)
    if len(used) != len(disclosures):
        raise _Invalid("unused disclosure: every disclosure must be referenced by a digest")

    mandates = resolved.get("delegate_payload")
    if (
        not isinstance(mandates, list)
        or len(mandates) != 1
        or not isinstance(mandates[0], dict)
        or mandates[0].get("vct") != PAYMENT_MANDATE_VCT
    ):
        raise _Invalid(f"delegate_payload must hold exactly one {PAYMENT_MANDATE_VCT} mandate")
    return mandates[0]


def _verify_signature(signing_input: bytes, signature: bytes, header: dict, trusted_jwks: dict) -> None:
    if len(signature) != 64:
        raise _Invalid("signature does not verify: ES256 signature must be 64 bytes")
    der = encode_dss_signature(int.from_bytes(signature[:32], "big"), int.from_bytes(signature[32:], "big"))
    kid = header.get("kid")
    for jwk in trusted_jwks.get("keys", []):
        if jwk.get("kty") != "EC" or jwk.get("crv") != "P-256":
            continue
        if kid is not None and jwk.get("kid") not in (None, kid):
            continue
        try:
            key = ec.EllipticCurvePublicNumbers(
                int.from_bytes(_b64decode(jwk["x"]), "big"),
                int.from_bytes(_b64decode(jwk["y"]), "big"),
                ec.SECP256R1(),
            ).public_key()
            key.verify(der, signing_input, ec.ECDSA(hashes.SHA256()))
            return
        except (KeyError, ValueError, InvalidSignature, _Invalid):
            continue
    raise _Invalid("signature does not verify with any trusted key")


def _check_time(payload: dict, now: datetime) -> None:
    exp, iat = payload.get("exp"), payload.get("iat")
    for name, value in (("exp", exp), ("iat", iat)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _Invalid(f"missing or non-numeric {name}")
    ts = now.timestamp()
    if ts > exp + CLOCK_SKEW_SECONDS:
        raise _Invalid("intent expired (exp)")
    if iat > ts + CLOCK_SKEW_SECONDS:
        raise _Invalid("intent issued in the future (iat)")


def _index(disclosures: List[str]) -> Dict[str, Tuple[str, list]]:
    """digest -> (disclosure, decoded [salt, (name,) value])."""
    index = {}
    for d in disclosures:
        decoded = _json(d)
        if not isinstance(decoded, list) or len(decoded) not in (2, 3):
            raise _Invalid("malformed disclosure: expected [salt, value] or [salt, name, value]")
        digest = base64.urlsafe_b64encode(hashlib.sha256(d.encode()).digest()).rstrip(b"=").decode()
        if digest in index:
            raise _Invalid("duplicate disclosure")
        index[digest] = (d, decoded)
    return index


def _take(digest: Any, index: Dict[str, Tuple[str, list]], used: set, size: int) -> list:
    if not isinstance(digest, str) or digest not in index:
        raise _Invalid("digest has no matching disclosure (hidden or tampered)")
    if digest in used:
        raise _Invalid("digest referenced more than once")
    disclosure = index[digest][1]
    if len(disclosure) != size:
        raise _Invalid("disclosure kind does not match where its digest is used")
    used.add(digest)
    return disclosure


def _resolve(node: Any, index: Dict[str, Tuple[str, list]], used: set) -> Any:
    """Replace every digest with its disclosed value, recursively."""
    if isinstance(node, list):
        out = []
        for item in node:
            if isinstance(item, dict) and set(item) == {"..."}:
                item = _take(item["..."], index, used, 2)[1]
            out.append(_resolve(item, index, used))
        return out
    if isinstance(node, dict):
        out = {k: _resolve(v, index, used) for k, v in node.items() if k not in ("_sd", "_sd_alg")}
        sd = node.get("_sd", [])
        if not isinstance(sd, list):
            raise _Invalid("malformed SD-JWT: _sd is not an array")
        for digest in sd:
            _, name, value = _take(digest, index, used, 3)
            if not isinstance(name, str) or name in out or name in ("_sd", "..."):
                raise _Invalid(f"disclosed claim name {name!r} is invalid or already present")
            out[name] = _resolve(value, index, used)
        return out
    return node


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_constraints(mandate: dict, *, payee: str, amount_minor: int, currency: str) -> IntentDecision:
    constraints = mandate.get("constraints")
    if not isinstance(constraints, list) or not constraints or not all(isinstance(c, dict) for c in constraints):
        return _reject("mandate must hold a non-empty list of constraints")
    # Reject unsupported types before judging anything, so a constraint the user
    # set is never silently skipped.
    for c in constraints:
        if c.get("type") not in (AMOUNT, ALLOWED_PAYEE):
            return _reject(f"unsupported constraint {c.get('type')!r}", str(c.get("type")))

    for c in constraints:
        if c["type"] == AMOUNT:
            if c.get("currency") != currency:
                return _reject(f"currency {currency!r} does not match the intent's {c.get('currency')!r}", AMOUNT)
            low, high = c.get("min"), c.get("max")
            if any(v is not None and not _is_int(v) for v in (low, high)):
                return _reject("min / max must be integers in minor units", AMOUNT)
            if low is not None and amount_minor < low:
                return _reject(f"amount {amount_minor} is below min {low}", AMOUNT)
            if high is not None and amount_minor > high:
                return _reject(f"amount {amount_minor} is above max {high}", AMOUNT)
        else:
            payees = c.get("allowed_payees")
            if not isinstance(payees, list) or not payees:
                return _reject("allowed_payees is empty", ALLOWED_PAYEE)
            if not any(isinstance(p, dict) and p.get("id") == payee for p in payees):
                return _reject(f"payee {payee!r} is not in allowed_payees", ALLOWED_PAYEE)
    return IntentDecision(allowed=True, reason="within the signed intent")
