"""agent/scripts/measure_prompt_cache.py against a stand-in Bedrock Converse server (PBI #94, Task #225)."""
import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "measure_prompt_cache.py"


def _load():
    spec = importlib.util.spec_from_file_location("measure_prompt_cache", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def converse_server(monkeypatch):
    """Answers Converse; counts the cache points of each request and reports a cache read when there are any."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "unused")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "unused")
    monkeypatch.setenv("AWS_REGION_NAME", "ap-northeast-1")
    points = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            blocks = body.get("system", []) + [b for m in body["messages"] for b in m["content"]]
            n = sum(1 for b in blocks if "cachePoint" in b)
            points.append(n)
            usage = {"inputTokens": 100, "outputTokens": 2, "totalTokens": 102,
                     "cacheReadInputTokens": 6000 if n else 0, "cacheWriteInputTokens": 0}
            data = json.dumps({"output": {"message": {"role": "assistant", "content": [{"text": "ok"}]}},
                               "stopReason": "end_turn", "usage": usage, "metrics": {"latencyMs": 1}}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", points
    server.shutdown()
    server.server_close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cache", [False, True])
async def test_one_record_per_request_and_cache_points_only_with_the_config(converse_server, cache):
    base, points = converse_server
    module = _load()

    records = await module.run("bedrock/apac.amazon.nova-micro-v1:0", cache, base)

    assert len(records) == len(module.TURNS) == len(points)  # one request per turn: the agent answers without tools
    assert points == [2 if cache else 0] * len(points)  # system and the last message, only with ContextCacheConfig
    assert all(r["output"] == 2 and r["seconds"] >= 0 for r in records)
    assert [r["cache_read"] for r in records] == [6000 if cache else 0] * len(records)
