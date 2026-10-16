import logging
import os
import sys
import tempfile
import unittest

from conftest import shared_client

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import main

HEADERS = {"Accept": "application/json, text/event-stream", "Host": "localhost:8001"}
TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
TRACEPARENT = f"00-{TRACE_ID}-00f067aa0ba902b7-01"
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


class TestToolCallLog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._client = shared_client()

    def _call(self, path, meta=None, session="s-630", arguments=None):
        headers = {**HEADERS, "X-DAK-Session-Key": session}
        params = {"name": "read_file", "arguments": {"path": path} if arguments is None else arguments}
        if meta:
            params["_meta"] = meta
        init = self._client.post("/mcp", json=INITIALIZE, headers=headers)
        headers["mcp-session-id"] = init.headers["mcp-session-id"]
        self._client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=headers)
        with self.assertLogs("dak.mcp.tool_call", logging.INFO) as cm:
            resp = self._client.post(
                "/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": params}, headers=headers
            )
        return resp, cm.output

    def _existing_file(self):
        f = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
        f.write("x")
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_with_traceparent(self):
        resp, out = self._call(self._existing_file(), {"traceparent": TRACEPARENT})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(out), 1)
        self.assertIn("tool_call name=read_file session=s-630 " + f"trace_id={TRACE_ID} ok=true", out[0])

    def test_without_traceparent(self):
        resp, out = self._call(self._existing_file())
        self.assertEqual(resp.status_code, 200)
        self.assertIn("trace_id=- ok=true", out[0])

    def test_caller_chosen_name_stays_on_one_line(self):
        headers = {**HEADERS, "X-DAK-Session-Key": "s-630"}
        init = self._client.post("/mcp", json=INITIALIZE, headers=headers)
        headers["mcp-session-id"] = init.headers["mcp-session-id"]
        self._client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=headers)
        params = {"name": "x\ntool_call name=forged ok=true", "arguments": {}}
        with self.assertLogs("dak.mcp.tool_call", logging.INFO) as cm:
            self._client.post(
                "/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": params}, headers=headers
            )
        self.assertEqual(len(cm.output), 1)
        self.assertNotIn("\n", cm.output[0])
        self.assertIn("ok=false", cm.output[0])

    def test_failed_call_logs_ok_false(self):
        # read_file reports a missing file as ordinary text (ok); a call the server rejects is a failure.
        _, out = self._call(None, {"traceparent": TRACEPARENT}, arguments={})
        self.assertIn("ok=false", out[0])


if __name__ == "__main__":
    unittest.main()
