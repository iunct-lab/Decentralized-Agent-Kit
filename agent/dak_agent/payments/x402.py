"""x402（HTTP 402 の支払い要求）の試作。

402 応答を「支払いが要る」Observation にし、明示的に承認されたときだけ mock 署名を付けて
再試行する。ツールには登録しておらず、エージェントの挙動は変えない（PBI #306 の Spike）。
402 を見て自動で払うことはしない（設計原則 1: System ENABLES, Agent DECIDES）。

仕様: https://github.com/coinbase/x402 の specs/x402-specification-v2.md と
specs/transports-v2/http.md（v2: PAYMENT-REQUIRED / PAYMENT-SIGNATURE ヘッダ）、
specs/x402-specification-v1.md（v1: 本文の JSON）。
"""

import base64
import binascii
import json
from dataclasses import dataclass, field
from typing import Any

import httpx

PAYMENT_REQUIRED_HEADER = "PAYMENT-REQUIRED"
PAYMENT_SIGNATURE_HEADER = "PAYMENT-SIGNATURE"

# 署名の代わりに入れる固定の値。鍵は使わない
MOCK_PAYLOAD = {"mock": True}


@dataclass(frozen=True)
class X402Requirement:
    """402 応答の `accepts` の 1 件。amount は最小単位の文字列のまま持つ。"""

    scheme: str
    network: str
    amount: str
    asset: str
    pay_to: str
    max_timeout_seconds: int
    resource: str
    x402_version: int
    extra: dict[str, Any] = field(default_factory=dict)


def _decode_header(value: str) -> dict:
    try:
        data = json.loads(base64.b64decode(value, validate=True))
    except (binascii.Error, ValueError) as e:
        raise ValueError(f"{PAYMENT_REQUIRED_HEADER} is not base64 JSON: {e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"{PAYMENT_REQUIRED_HEADER} is not a JSON object")
    return data


def _text(item: dict, key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"accepts item field {key!r} must be a non-empty string")
    return value


def _requirement(item: Any, version: int, resource: str) -> X402Requirement:
    if not isinstance(item, dict):
        raise ValueError("accepts item is not an object")
    # v1 は maxAmountRequired、v2 は amount。どちらも最小単位の整数を文字列で持つ
    amount = _text(item, "amount" if version == 2 else "maxAmountRequired")
    if not amount.isdigit():
        raise ValueError(f"amount must be an integer string in atomic units: {amount!r}")
    timeout = item.get("maxTimeoutSeconds")
    if type(timeout) is not int:
        raise ValueError(f"maxTimeoutSeconds must be an integer: {timeout!r}")
    extra = item.get("extra") or {}
    if not isinstance(extra, dict):
        raise ValueError("extra must be an object")
    return X402Requirement(
        scheme=_text(item, "scheme"),
        network=_text(item, "network"),
        amount=amount,
        asset=_text(item, "asset"),
        pay_to=_text(item, "payTo"),
        max_timeout_seconds=timeout,
        # v1 は要求ごとに resource（URL）を持つ
        resource=resource if version == 2 else _text(item, "resource"),
        x402_version=version,
        extra=extra,
    )


def parse_payment_required(response: httpx.Response) -> list[X402Requirement]:
    """402 応答から支払い方法の一覧を読む。v2 はヘッダ、v1 は本文。402 以外や壊れた入力は ValueError。"""
    if response.status_code != 402:
        raise ValueError(f"not a 402 response: {response.status_code}")

    header = response.headers.get(PAYMENT_REQUIRED_HEADER)
    if header is not None:
        data = _decode_header(header)
    else:
        try:
            data = response.json()
        except ValueError as e:
            raise ValueError(f"402 body is not JSON: {e}") from e
        if not isinstance(data, dict):
            raise ValueError("402 body is not a JSON object")

    version = data.get("x402Version")
    if version not in (1, 2):
        raise ValueError(f"unsupported x402Version: {version!r}")
    accepts = data.get("accepts")
    if not isinstance(accepts, list) or not accepts:
        raise ValueError("accepts is missing or empty")

    resource = ""
    if version == 2:
        info = data.get("resource")
        resource = str(info.get("url") or "") if isinstance(info, dict) else ""
        if not resource:
            raise ValueError("resource.url is missing")
    return [_requirement(item, version, resource) for item in accepts]


def format_x402_observation(url: str, reqs: list[X402Requirement]) -> dict[str, str]:
    """PaymentHandler.format_payment_error と同じ `{"error": ...}` の形の Observation。支払いは勧めない。"""
    lines = [
        f"Payment Required: {url} answered HTTP 402 (x402).",
        "",
        "Accepted payment options (amount is in the asset's smallest unit):",
    ]
    for i, r in enumerate(reqs, 1):
        lines.append(
            f"{i}. scheme={r.scheme} network={r.network} amount={r.amount} asset={r.asset} "
            f"payTo={r.pay_to} expires_in={r.max_timeout_seconds}s"
        )
    lines += [
        "",
        "Nothing has been paid or signed. Whether to pay is decided according to the user's permission; "
        "without that permission, do not pay and report this requirement to the user.",
    ]
    return {"error": "\n".join(lines)}


def retry_with_payment(
    client: httpx.Client, url: str, requirement: X402Requirement, *, approved: bool
) -> httpx.Response:
    """承認されたときだけ、mock 署名の PAYMENT-SIGNATURE を付けて 1 回だけ再送する。未承認なら何も送らない。"""
    if approved is not True:
        raise PermissionError("x402 payment was not approved; nothing was signed or sent")
    accepted = {
        "scheme": requirement.scheme,
        "network": requirement.network,
        "amount": requirement.amount,
        "asset": requirement.asset,
        "payTo": requirement.pay_to,
        "maxTimeoutSeconds": requirement.max_timeout_seconds,
        "extra": requirement.extra,
    }
    payload = {"x402Version": 2, "accepted": accepted, "payload": MOCK_PAYLOAD}
    signature = base64.b64encode(json.dumps(payload).encode()).decode()
    return client.get(url, headers={PAYMENT_SIGNATURE_HEADER: signature})
