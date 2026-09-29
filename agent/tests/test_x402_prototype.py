"""x402 の試作（dak_agent.payments.x402）のテスト。

入力は仕様の例をそのまま使う（https://github.com/coinbase/x402 の
specs/transports-v2/http.md と specs/x402-specification-v1.md、2026-09-29 確認）。
有料サーバは httpx.MockTransport の偽物で、実ネットワークには送らない。
"""

import base64
import json

import httpx
import pytest

from dak_agent.payments.x402 import (
    X402Requirement,
    format_x402_observation,
    parse_payment_required,
    retry_with_payment,
)

URL = "https://api.example.com/premium-data"

# specs/transports-v2/http.md の PAYMENT-REQUIRED ヘッダの例（そのまま）
V2_PAYMENT_REQUIRED = (
    "eyJ4NDAyVmVyc2lvbiI6MiwiZXJyb3IiOiJQQVlNRU5ULVNJR05BVFVSRSBoZWFkZXIgaXMgcmVxdWlyZWQiLCJyZXNvdXJjZSI6eyJ1cmwiOiJodHRwczovL2FwaS5leGFtcGxlLmNvbS9wcmVtaXVtLWRhdGEiLCJkZXNjcmlwdGlvbiI6IkFjY2VzcyB0byBwcmVtaXVtIG1hcmtldCBkYXRhIiwibWltZVR5cGUiOiJhcHBsaWNhdGlvbi9qc29uIn0sImFjY2VwdHMiOlt7InNjaGVtZSI6ImV4YWN0IiwibmV0d29yayI6ImVpcDE1NTo4NDUzMiIsImFtb3VudCI6IjEwMDAwIiwiYXNzZXQiOiIweDAzNkNiRDUzODQyYzU0MjY2MzRlNzkyOTU0MWVDMjMxOGYzZENGN2UiLCJwYXlUbyI6IjB4MjA5NjkzQmM2YWZjMEM1MzI4YkEzNkZhRjAzQzUxNEVGMzEyMjg3QyIsIm1heFRpbWVvdXRTZWNvbmRzIjo2MCwiZXh0cmEiOnsibmFtZSI6IlVTREMiLCJ2ZXJzaW9uIjoiMiJ9fV19"
)

# specs/transports-v2/http.md の PAYMENT-RESPONSE ヘッダの例（成功）
V2_PAYMENT_RESPONSE = (
    "eyJzdWNjZXNzIjp0cnVlLCJ0cmFuc2FjdGlvbiI6IjB4MTIzNDU2Nzg5MGFiY2RlZjEyMzQ1Njc4OTBhYmNkZWYxMjM0NTY3ODkwYWJjZGVmMTIzNDU2Nzg5MGFiY2RlZiIsIm5ldHdvcmsiOiJlaXAxNTU6ODQ1MzIiLCJwYXllciI6IjB4ODU3YjA2NTE5RTkxZTNBNTQ1Mzg3OTFiRGJiMEUyMjM3M2UzNmI2NiJ9"
)

# specs/x402-specification-v1.md 5.1.1 の本文の例（そのまま）
V1_BODY = {
    "x402Version": 1,
    "error": "X-PAYMENT header is required",
    "accepts": [
        {
            "scheme": "exact",
            "network": "base-sepolia",
            "maxAmountRequired": "10000",
            "asset": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
            "payTo": "0x209693Bc6afc0C5328bA36FaF03C514EF312287C",
            "resource": "https://api.example.com/premium-data",
            "description": "Access to premium market data",
            "mimeType": "application/json",
            "outputSchema": None,
            "maxTimeoutSeconds": 60,
            "extra": {"name": "USDC", "version": "2"},
        }
    ],
}

ASSET = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
PAY_TO = "0x209693Bc6afc0C5328bA36FaF03C514EF312287C"


def _b64json(value: str) -> dict:
    return json.loads(base64.b64decode(value))


class FakePaidServer:
    """ヘッダなし → 402 と PAYMENT-REQUIRED、PAYMENT-SIGNATURE あり → 200 と PAYMENT-RESPONSE。"""

    def __init__(self):
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if "PAYMENT-SIGNATURE" in request.headers:
            return httpx.Response(200, headers={"PAYMENT-RESPONSE": V2_PAYMENT_RESPONSE}, json={"data": "premium"})
        return httpx.Response(402, headers={"PAYMENT-REQUIRED": V2_PAYMENT_REQUIRED}, json={})


@pytest.fixture
def server():
    return FakePaidServer()


@pytest.fixture
def client(server):
    with httpx.Client(transport=httpx.MockTransport(server)) as c:
        yield c


def test_parse_v2_header(client):
    reqs = parse_payment_required(client.get(URL))
    assert reqs == [
        X402Requirement(
            scheme="exact",
            network="eip155:84532",
            amount="10000",
            asset=ASSET,
            pay_to=PAY_TO,
            max_timeout_seconds=60,
            resource=URL,
            x402_version=2,
            extra={"name": "USDC", "version": "2"},
        )
    ]


def test_parse_v1_body():
    resp = httpx.Response(402, json=V1_BODY, request=httpx.Request("GET", URL))
    reqs = parse_payment_required(resp)
    assert reqs == [
        X402Requirement(
            scheme="exact",
            network="base-sepolia",
            amount="10000",
            asset=ASSET,
            pay_to=PAY_TO,
            max_timeout_seconds=60,
            resource=URL,
            x402_version=1,
            extra={"name": "USDC", "version": "2"},
        )
    ]


@pytest.mark.parametrize(
    "resp",
    [
        httpx.Response(200, json={}),
        httpx.Response(402, text="not json"),
        httpx.Response(402, json={"x402Version": 1}),
        httpx.Response(402, json={"x402Version": 3, "accepts": []}),
        httpx.Response(402, headers={"PAYMENT-REQUIRED": "!!!"}),
        httpx.Response(402, headers={"PAYMENT-REQUIRED": base64.b64encode(b'{"x402Version":2,"accepts":[{}]}').decode()}),
        httpx.Response(
            402,
            headers={"PAYMENT-REQUIRED": base64.b64encode(b'{"x402Version":2,"resource":{"url":"u"},"accepts":[{}]}').decode()},
        ),
    ],
    ids=["not-402", "body-not-json", "no-accepts", "unknown-version", "header-not-base64", "missing-resource", "missing-fields"],
)
def test_parse_rejects_broken_input(resp):
    with pytest.raises(ValueError):
        parse_payment_required(resp)


def test_observation_lists_requirements_without_recommending_payment(client):
    reqs = parse_payment_required(client.get(URL))
    obs = format_x402_observation(URL, reqs)
    assert set(obs) == {"error"}
    msg = obs["error"]
    assert msg.startswith("Payment Required:")
    for fact in (URL, "10000", ASSET, "eip155:84532", PAY_TO, "60"):
        assert fact in msg
    assert "permission" in msg.lower()


def test_no_approval_sends_nothing(client, server):
    reqs = parse_payment_required(client.get(URL))
    with pytest.raises(PermissionError):
        retry_with_payment(client, URL, reqs[0], approved=False)
    assert len(server.requests) == 1
    assert "PAYMENT-SIGNATURE" not in server.requests[0].headers


def test_approved_retries_once_with_mock_signature(client, server):
    reqs = parse_payment_required(client.get(URL))
    resp = retry_with_payment(client, URL, reqs[0], approved=True)

    assert resp.status_code == 200
    assert len(server.requests) == 2
    payload = _b64json(server.requests[1].headers["PAYMENT-SIGNATURE"])
    assert payload["x402Version"] == 2
    assert payload["accepted"]["amount"] == "10000"
    assert payload["accepted"]["payTo"] == PAY_TO
    assert payload["accepted"]["network"] == "eip155:84532"
    assert payload["payload"] == {"mock": True}
    assert _b64json(resp.headers["PAYMENT-RESPONSE"])["success"] is True


def test_approved_is_keyword_only_without_default(client):
    reqs = parse_payment_required(client.get(URL))
    with pytest.raises(TypeError):
        retry_with_payment(client, URL, reqs[0])  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        retry_with_payment(client, URL, reqs[0], True)  # type: ignore[misc]
