"""Spike (PBI #318 / Task #320): publish DAK's agent and mcp-server with DNS-AID, then find them again.

Publishes `dak-agent` (A2A) and `dak-mcp` (MCP) as SVCB records into the test zone of
dns-aid-core's BIND9 container (RFC 2136 update, TSIG), discovers them with `dns_aid.discover`,
and from what was found fetches DAK's Agent Card and the MCP `initialize` response.

Not production code. Run it as README.md in this directory says. The TSIG key comes from the
environment (DDNS_KEY_NAME / DDNS_KEY_SECRET), never from a file in this repository.
"""
import asyncio
import json
import logging
import os
import time

import dns.asyncresolver
import dns.resolver
import httpx
import structlog

import dns_aid
from dns_aid.backends.ddns import DDNSBackend
from dns_aid.core.indexer import IndexEntry, update_index

ZONE = os.environ.get("DNSAID_ZONE", "test.dns-aid.local")
DNS_SERVER = os.environ.get("DDNS_SERVER", "127.0.0.1")
DNS_PORT = int(os.environ.get("DDNS_PORT", "15353"))
DAK_HOST = os.environ.get("DAK_HOST", "localhost")
AGENT_PORT = int(os.environ.get("DAK_AGENT_PORT", "8000"))
MCP_PORT = int(os.environ.get("DAK_MCP_PORT", "8001"))
CARD_PATH = "/a2a/dak_agent/.well-known/agent-card.json"
MCP_PATH = "/mcp"  # DNS-AID has no SvcParam for an MCP endpoint's path; DAK's default is used


class _TestBindResolver(dns.asyncresolver.Resolver):
    """`dns_aid.discover` builds its own resolver from the host's resolv.conf and takes no
    nameserver argument, so the spike points every new resolver at the test BIND."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.nameservers = [DNS_SERVER]
        self.port = DNS_PORT


async def publish(backend: DDNSBackend) -> None:
    entries = []
    for name, protocol, port, well_known in (
        ("dak-agent", "a2a", AGENT_PORT, CARD_PATH),
        ("dak-mcp", "mcp", MCP_PORT, None),
    ):
        result = await dns_aid.publish(
            name=name, domain=ZONE, protocol=protocol, endpoint=DAK_HOST, port=port,
            well_known_path=well_known, ttl=60, backend=backend,
        )
        print(f"published {name}: success={result.success} records={result.records_created}")
        entries.append(IndexEntry(name=name, protocol=protocol))
    # Without the organization index (_index._agents.<zone>) discover() lists nothing in this zone.
    index = await update_index(ZONE, backend, add=entries, ttl=60)
    print(f"index: success={index.success} entries={[f'{e.name}/{e.protocol}' for e in index.entries]}")


async def discover(protocol: str):
    started = time.perf_counter()
    result = await dns_aid.discover(ZONE, protocol=protocol, enrich_endpoints=False)
    elapsed_ms = (time.perf_counter() - started) * 1000
    print(f"discover({protocol}): {len(result.agents)} agent(s), dnssec_validated={result.dnssec_validated}, "
          f"query_time_ms={result.query_time_ms:.1f}, wall_ms={elapsed_ms:.1f}")
    for agent in result.agents:
        print(f"  {agent.fqdn} target={agent.target_host} port={agent.port} "
              f"well_known={agent.well_known_path} endpoint_url={agent.endpoint_url}")
    return result.agents


async def main() -> None:
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))  # dns-aid logs to stdout
    dns.asyncresolver.Resolver = _TestBindResolver
    default = dns.resolver.get_default_resolver()  # the DDNS backend reads records back with this one
    default.nameservers, default.port = [DNS_SERVER], DNS_PORT
    backend = DDNSBackend(server=DNS_SERVER, port=DNS_PORT)  # key from DDNS_KEY_NAME / DDNS_KEY_SECRET
    await publish(backend)

    a2a = [a for a in await discover("a2a") if a.name == "dak-agent"]
    mcp = [a for a in await discover("mcp") if a.name == "dak-mcp"]
    if not a2a or not mcp:
        raise SystemExit("not found: dak-agent or dak-mcp")
    try:  # the test zone is not signed and the test BIND sets no AD flag
        await dns_aid.discover(ZONE, protocol="a2a", enrich_endpoints=False, require_dnssec=True)
        print("discover(a2a, require_dnssec=True): accepted")
    except dns_aid.DNSSECError as e:
        print(f"discover(a2a, require_dnssec=True): DNSSECError: {e}")

    # dns-aid builds https://<target>:<port> (and the descriptor URL without the port); the local
    # DAK serves plain http, so the spike builds the URLs from target, port and path itself.
    agent, server = a2a[0], mcp[0]
    async with httpx.AsyncClient(timeout=10) as http:
        card = (await http.get(f"http://{agent.target_host}:{agent.port}{agent.well_known_path}")).json()
        print(f"agent card: name={card['name']} interfaces={[i['url'] for i in card.get('supportedInterfaces', [])]}")

        init = await http.post(
            f"http://{server.target_host}:{server.port}{MCP_PATH}",
            headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json"},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "dak-discovery-spike", "version": "0"}}},
        )
        body = init.text
        if body.lstrip().startswith("event:") or "data:" in body:  # streamable HTTP may answer as SSE
            body = next(line[5:] for line in body.splitlines() if line.startswith("data:"))
        print(f"mcp initialize: serverInfo={json.dumps(json.loads(body)['result']['serverInfo'])}")


if __name__ == "__main__":
    asyncio.run(main())
