"""スキル x402 のツールのテスト。

有料サーバは httpx.MockTransport の偽物で、実ネットワークには送らない。
"""

import base64
import importlib.util
import json
import os
from types import SimpleNamespace

import httpx
import pytest

from dak_agent.skill_registry import SkillRegistry

SKILLS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../..", "skills"))
_spec = importlib.util.spec_from_file_location(
    "x402_skill_tools", os.path.join(SKILLS_DIR, "x402", "tools.py")
)
tools = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tools)

URL = "https://paid.example.com/data"
PAYMENT_REQUIRED = {
    "x402Version": 2,
    "resource": {"url": URL},
    "accepts": [
        {
            "scheme": "exact",
            "network": "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1",
            "amount": "10000",
            "asset": "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU",
            "payTo": "2wKupLR9q6wXYppw8Gr2NvWxKBUqm4PPJKkQfoxHDBg4",
            "maxTimeoutSeconds": 60,
            "extra": {"feePayer": "EwWqGE4ZFKLofuestmU4LDdK7XM1N4ALgdZccwYugwGd"},
        }
    ],
}


@pytest.fixture
def paid_server(monkeypatch):
    """402 か 200 を返す偽の有料サーバ。受けた要求を requests に残す。"""
    requests: list[httpx.Request] = []
    state = {"status": 402}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if state["status"] == 402:
            header = base64.b64encode(json.dumps(PAYMENT_REQUIRED).encode()).decode()
            return httpx.Response(402, headers={"PAYMENT-REQUIRED": header}, json={})
        return httpx.Response(200, text="premium data")

    real_client = httpx.Client
    monkeypatch.setattr(
        tools.httpx, "Client",
        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
    )
    return SimpleNamespace(requests=requests, state=state)


def _tool_context():
    return SimpleNamespace(state={})


def test_fetch_url_402_returns_observation_without_paying(paid_server):
    ctx = _tool_context()

    out = tools.fetch_url(URL, ctx)

    assert "Payment Required" in out
    for text in ("amount=10000", "asset=4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU",
                 "network=solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1",
                 "payTo=2wKupLR9q6wXYppw8Gr2NvWxKBUqm4PPJKkQfoxHDBg4", "expires_in=60s"):
        assert text in out
    assert len(paid_server.requests) == 1
    assert not any("PAYMENT-SIGNATURE" in r.headers for r in paid_server.requests)
    [req] = ctx.state["x402_requirements"][URL]
    assert req["amount"] == "10000" and req["pay_to"] == "2wKupLR9q6wXYppw8Gr2NvWxKBUqm4PPJKkQfoxHDBg4"


def test_fetch_url_402_keeps_other_urls_in_state(paid_server):
    ctx = _tool_context()
    ctx.state["x402_requirements"] = {"https://other.example.com/": []}

    tools.fetch_url(URL, ctx)

    assert set(ctx.state["x402_requirements"]) == {"https://other.example.com/", URL}


def test_fetch_url_200_returns_body(paid_server):
    paid_server.state["status"] = 200
    ctx = _tool_context()

    assert tools.fetch_url(URL, ctx) == "HTTP 200\npremium data"
    assert "x402_requirements" not in ctx.state


def test_fetch_url_broken_402_reports_reason(monkeypatch):
    real_client = httpx.Client
    monkeypatch.setattr(
        tools.httpx, "Client",
        lambda **kw: real_client(transport=httpx.MockTransport(lambda r: httpx.Response(402, text="pay")), **kw),
    )
    ctx = _tool_context()

    out = tools.fetch_url(URL, ctx)

    assert "could not be read" in out
    assert "x402_requirements" not in ctx.state


def test_x402_skill_loads():
    registry = SkillRegistry([SKILLS_DIR])
    registry.load_skills()

    assert "fetch_url" in registry.skills["x402"]["tools"]


def test_fetch_url_does_not_follow_redirects(monkeypatch):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(302, headers={"Location": "https://elsewhere.example.com/"})

    real_client = httpx.Client
    monkeypatch.setattr(
        tools.httpx, "Client",
        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
    )

    assert tools.fetch_url(URL, _tool_context()).startswith("HTTP 302")
    assert len(seen) == 1
