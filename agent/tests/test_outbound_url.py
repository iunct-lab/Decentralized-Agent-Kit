"""check_outbound_url / PinnedTransport: URLs given to tools must not reach internal addresses (#728)."""
import socket

import httpx
import pytest

from dak_agent import outbound_url
from dak_agent.outbound_url import OutboundURLRefused, PinnedTransport, check_outbound_url


def _dns(monkeypatch, mapping):
    """Fake getaddrinfo: host -> list of address strings (or a callable returning the list)."""

    def fake(host, port, *args, **kwargs):
        addrs = mapping(host) if callable(mapping) else mapping[host]
        return [
            (socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port or 0))
            for a in addrs
        ]

    monkeypatch.setattr(outbound_url.socket, "getaddrinfo", fake)


@pytest.mark.parametrize("url", ["http://127.0.0.1/", "http://[::1]/", "http://0.0.0.0/"])
def test_refuses_loopback(url):
    with pytest.raises(OutboundURLRefused):
        check_outbound_url(url)


@pytest.mark.parametrize("url", ["http://10.0.0.5/", "http://192.168.1.1/", "http://172.16.0.1/"])
def test_refuses_private(url):
    with pytest.raises(OutboundURLRefused):
        check_outbound_url(url)


@pytest.mark.parametrize("url", ["http://169.254.169.254/latest/meta-data/", "http://[fd00:ec2::254]/"])
def test_refuses_link_local_metadata(url):
    with pytest.raises(OutboundURLRefused):
        check_outbound_url(url)


@pytest.mark.parametrize("url", [
    "http://[64:ff9b::7f00:1]/", "http://[64:ff9b::a9fe:a9fe]/", "http://[::127.0.0.1]/",
    "http://[::10.0.0.1]/", "http://[::ffff:0:7f00:1]/", "http://[fec0::1]/",
])
def test_refuses_ipv6_forms_embedding_internal_ipv4(url):
    with pytest.raises(OutboundURLRefused):
        check_outbound_url(url)


def test_refuses_label_too_long_without_raising_other_errors():
    with pytest.raises(OutboundURLRefused):
        check_outbound_url("http://" + "a" * 64 + ".com/")


def test_returns_ipv4_before_ipv6(monkeypatch):
    _dns(monkeypatch, {"dual.example.com": ["2606:4700::1111", "93.184.216.34"]})
    assert check_outbound_url("https://dual.example.com/") == ["93.184.216.34", "2606:4700::1111"]


def test_refuses_ipv4_mapped_loopback():
    with pytest.raises(OutboundURLRefused):
        check_outbound_url("http://[::ffff:127.0.0.1]/")


@pytest.mark.parametrize("url", ["http://mcp-server:8001/", "http://localhost/", "http://postgres/"])
def test_refuses_single_label_host(url):
    with pytest.raises(OutboundURLRefused):
        check_outbound_url(url)


def test_refuses_name_resolving_to_private(monkeypatch):
    _dns(monkeypatch, {"evil.example.com": ["10.0.0.5"]})
    with pytest.raises(OutboundURLRefused) as e:
        check_outbound_url("https://evil.example.com/")
    assert "10.0.0.5" in str(e.value)


def test_refuses_if_any_address_is_private(monkeypatch):
    _dns(monkeypatch, {"mixed.example.com": ["93.184.216.34", "10.0.0.5"]})
    with pytest.raises(OutboundURLRefused):
        check_outbound_url("https://mixed.example.com/")


@pytest.mark.parametrize("url", ["file:///etc/passwd", "gopher://example.com/", "ftp://example.com/"])
def test_refuses_non_http_scheme(url):
    with pytest.raises(OutboundURLRefused):
        check_outbound_url(url)


def test_refuses_unresolvable(monkeypatch):
    def boom(*a, **k):
        raise socket.gaierror("nope")

    monkeypatch.setattr(outbound_url.socket, "getaddrinfo", boom)
    with pytest.raises(OutboundURLRefused):
        check_outbound_url("https://nx.example.com/")


def test_allows_public_address(monkeypatch):
    _dns(monkeypatch, {"example.com": ["93.184.216.34"]})
    assert check_outbound_url("https://example.com/x") == ["93.184.216.34"]


def _capture(monkeypatch):
    seen = []

    def fake_handle(self, request):
        seen.append(request)
        return httpx.Response(200, request=request, text="ok")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", fake_handle)
    return seen


def test_transport_connects_to_checked_address(monkeypatch):
    _dns(monkeypatch, {"example.com": ["93.184.216.34"]})
    seen = _capture(monkeypatch)
    resp = PinnedTransport().handle_request(httpx.Request("GET", "https://example.com/p?q=1"))
    assert resp.status_code == 200
    (req,) = seen
    assert req.url.host == "93.184.216.34"
    assert req.headers["host"] == "example.com"
    assert req.extensions["sni_hostname"] == "example.com"


def test_transport_refuses_rebinding(monkeypatch):
    calls = {"n": 0}

    def answer(host):
        calls["n"] += 1
        return ["93.184.216.34"] if calls["n"] == 1 else ["10.0.0.5"]

    _dns(monkeypatch, answer)
    seen = _capture(monkeypatch)
    assert check_outbound_url("https://rebind.example.com/") == ["93.184.216.34"]
    with pytest.raises(OutboundURLRefused):
        PinnedTransport().handle_request(httpx.Request("GET", "https://rebind.example.com/"))
    assert seen == []


def test_outbound_client_does_not_follow_redirects_or_use_env_proxy():
    c = outbound_url.outbound_client()
    assert c.follow_redirects is False
    assert isinstance(c._transport, PinnedTransport)
