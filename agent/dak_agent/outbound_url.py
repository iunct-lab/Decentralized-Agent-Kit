"""
Destination check for tools that take a URL and go outside (SSRF guard, #728).

A URL is refused unless its scheme is http/https, its host is not a single-label name
(`localhost`, a compose service name), and every address the host resolves to is a public
address (loopback, private, link-local incl. cloud metadata, CGNAT, reserved are refused).
`PinnedTransport` connects to the address that was checked, so a DNS answer that changes
after the check (DNS rebinding) cannot send the request inside.

Tools that take a URL must use `outbound_client()`; do not build an httpx client yourself.
Create one client per request (connections are pooled by the pinned address, not the name).
"""
import ipaddress
import socket

import httpx


class OutboundURLRefused(ValueError):
    """The destination is not allowed. `str(e)` is the reason shown to the user and the model."""


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


# IPv6 ranges that carry an IPv4 address in the low 32 bits (NAT64, IPv4-compatible, SIIT)
_EMBEDDING_NETS = tuple(ipaddress.ip_network(n) for n in ("64:ff9b::/96", "::/96", "::ffff:0:0:0/96"))
_SITE_LOCAL = ipaddress.ip_network("fec0::/10")


def check_outbound_url(url: str) -> list[str]:
    """Return the checked (public) addresses of `url`, or raise OutboundURLRefused."""
    try:
        parsed = httpx.URL(url)
    except Exception as e:  # httpx.InvalidURL and friends
        raise OutboundURLRefused(f"refused: {url!r} is not a valid URL ({e})") from None
    if parsed.scheme not in ("http", "https"):
        raise OutboundURLRefused(f"refused: {url} uses scheme {parsed.scheme!r}; only http and https are allowed")
    host = parsed.host
    if not host:
        raise OutboundURLRefused(f"refused: {url} has no host")
    if "." not in host and not _is_ip_literal(host):
        raise OutboundURLRefused(
            f"refused: {url} has the single-label host {host!r} (localhost or an internal service name)"
        )
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError) as e:
        raise OutboundURLRefused(f"refused: {url} could not be resolved ({e})") from None
    addresses: list[str] = []
    for info in infos:
        raw = info[4][0].split("%", 1)[0]
        ip = ipaddress.ip_address(raw)
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        if isinstance(ip, ipaddress.IPv6Address):
            if any(ip in net for net in _EMBEDDING_NETS):
                ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
            elif ip in _SITE_LOCAL:
                raise OutboundURLRefused(f"refused: {url} resolves to {ip}, a site-local address")
        if not ip.is_global:
            raise OutboundURLRefused(
                f"refused: {url} resolves to {ip}, which is not a public address "
                "(loopback / private / link-local / cloud metadata / reserved)"
            )
        if raw not in addresses:
            addresses.append(raw)
    if not addresses:
        raise OutboundURLRefused(f"refused: {url} did not resolve to any address")
    # IPv4 first: a host without an IPv6 route still connects to an address that was checked
    return sorted(addresses, key=lambda a: ":" in a)


class PinnedTransport(httpx.HTTPTransport):
    """Check the destination for every request and connect to the checked address."""

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        original_host = request.url.host
        addresses = check_outbound_url(str(request.url))
        host_header = request.headers.get("host")
        request.url = request.url.copy_with(host=addresses[0])
        if host_header is None:
            request.headers["host"] = original_host
        if request.url.scheme == "https":
            request.extensions["sni_hostname"] = original_host
        return super().handle_request(request)


def outbound_client(**kwargs) -> httpx.Client:
    """httpx client for URLs that came from input: checked, pinned, no redirects, no env proxy."""
    return httpx.Client(transport=PinnedTransport(), trust_env=False, follow_redirects=False, **kwargs)
