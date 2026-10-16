import os
import sys
import unittest
from unittest.mock import patch

from conftest import shared_client

# Add parent directory to path to import main
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import main

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
HEADERS = {"Accept": "application/json, text/event-stream"}


class TestTransportSecurity(unittest.TestCase):
    """The agent reaches this server as `mcp-server:8000` inside docker compose.

    mcp >= 1.23 auto-enables DNS rebinding protection for the server's default
    host and rejects any non-localhost Host header with 421, which silently
    leaves the agent without MCP tools. Unit tests that call tool functions
    directly never send a Host header, so check the HTTP layer here.
    """

    @classmethod
    def setUpClass(cls):
        cls._client = shared_client()

    def _initialize(self, host: str) -> int:
        resp = self._client.post("/mcp", json=INITIALIZE, headers={**HEADERS, "Host": host})
        return resp.status_code

    def test_compose_service_host_is_accepted(self):
        self.assertEqual(self._initialize("mcp-server:8000"), 200)

    def test_localhost_is_accepted(self):
        self.assertEqual(self._initialize("localhost:8001"), 200)

    def test_unknown_host_is_rejected(self):
        self.assertEqual(self._initialize("evil.example:8000"), 421)

    def test_extra_hosts_from_env(self):
        with patch.dict(os.environ, {"MCP_ALLOWED_HOSTS": " mcp.internal:*, ,other:9000"}):
            hosts = main._allowed_hosts()
        self.assertEqual(hosts[-2:], ["mcp.internal:*", "other:9000"])
        self.assertIn("mcp-server:*", hosts)


if __name__ == "__main__":
    unittest.main()
