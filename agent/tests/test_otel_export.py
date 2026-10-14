"""#702: spans reach the OTLP endpoint set with OTEL_EXPORTER_OTLP_ENDPOINT, with or without LangFuse.

Each case runs ``setup_telemetry()`` in a subprocess (the global TracerProvider can be set
once per process) against small OTLP/HTTP sinks that run in this test.
"""
import http.server
import os
import socket
import subprocess
import sys
import threading

import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

CODE = """
from opentelemetry import trace
from dak_agent.patches import setup_telemetry
setup_telemetry()
with trace.get_tracer("dak-test").start_as_current_span("execute_tool echo") as s:
    s.set_attribute("gen_ai.operation.name", "execute_tool")
    s.set_attribute("gen_ai.tool.name", "echo")
trace.get_tracer_provider().force_flush()
p = trace.get_tracer_provider()
print(type(p).__name__, len(p._active_span_processor._span_processors))
"""


class Sink:
    def __init__(self):
        self.paths: list[str] = []
        self.spans: list[dict] = []
        sink = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                sink.paths.append(self.path)
                if self.path.endswith("/v1/traces"):
                    req = ExportTraceServiceRequest()
                    try:
                        req.ParseFromString(body)
                    except Exception:
                        pass
                    for rs in req.resource_spans:
                        for ss in rs.scope_spans:
                            for sp in ss.spans:
                                attrs = {kv.key: kv.value.string_value for kv in sp.attributes}
                                sink.spans.append({"trace_id": sp.trace_id.hex(), "name": sp.name, "attributes": attrs})
                self.send_response(200)
                self.end_headers()

            do_GET = do_POST

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def otlp_sink():
    s = Sink()
    yield s
    s.close()


langfuse_sink = otlp_sink  # a second, independent instance of the same fixture


def _run(**env):
    base = {k: v for k, v in os.environ.items() if not k.startswith(("LANGFUSE_", "OTEL_"))}
    base["OTEL_BSP_SCHEDULE_DELAY"] = "100"
    base.update(env)
    return subprocess.run([sys.executable, "-c", CODE], capture_output=True, text=True, check=True, env=base, timeout=60)


def _tools(sink):
    return [s for s in sink.spans if s["attributes"].get("gen_ai.operation.name") == "execute_tool"]


def test_otlp_only(otlp_sink):
    out = _run(OTEL_EXPORTER_OTLP_ENDPOINT=otlp_sink.url, OTEL_EXPORTER_OTLP_PROTOCOL="http/protobuf")
    assert out.stdout.split()[0] == "TracerProvider"
    assert "/v1/traces" in otlp_sink.paths
    assert [s["attributes"]["gen_ai.tool.name"] for s in _tools(otlp_sink)] == ["echo"]


def test_langfuse_and_otlp(otlp_sink, langfuse_sink):
    _run(OTEL_EXPORTER_OTLP_ENDPOINT=otlp_sink.url, OTEL_EXPORTER_OTLP_PROTOCOL="http/protobuf",
         LANGFUSE_PUBLIC_KEY="pk-lf-dummy", LANGFUSE_SECRET_KEY="sk-lf-dummy", LANGFUSE_BASE_URL=langfuse_sink.url)
    assert "/api/public/otel/v1/traces" in langfuse_sink.paths
    (a,), (b,) = _tools(otlp_sink), _tools(langfuse_sink)
    assert a["trace_id"] == b["trace_id"] != "0" * 32


def test_no_endpoint(otlp_sink):
    out = _run()
    assert out.stdout.split() == ["TracerProvider", "0"]  # no span processor, so no exporter
    assert otlp_sink.paths == []


def test_unreachable():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    out = _run(OTEL_EXPORTER_OTLP_ENDPOINT=f"http://127.0.0.1:{port}", OTEL_EXPORTER_OTLP_PROTOCOL="http/protobuf",
               OTEL_EXPORTER_OTLP_TIMEOUT="1")
    assert out.stdout.split()[0] == "TracerProvider"
