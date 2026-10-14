"""An OTLP/HTTP trace receiver for the integration tests. POST /v1/traces keeps the
spans it gets; GET /spans returns them as [{trace_id, name, attributes}]. The agent's
metrics and logs exports (POST /v1/metrics, /v1/logs) are accepted and dropped."""
import gzip
from typing import Any, Dict, List

import uvicorn
from fastapi import FastAPI, Request, Response
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

app = FastAPI(title="OTLP Sink")

_spans: List[Dict[str, Any]] = []


def _value(v) -> Any:
    kind = v.WhichOneof("value")
    if kind in ("string_value", "bool_value", "int_value", "double_value"):
        return getattr(v, kind)
    return str(v) if kind else None  # array / map / bytes: kept as text, only to be searched


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/v1/traces")
async def traces(request: Request):
    body = await request.body()
    if request.headers.get("content-encoding") == "gzip":
        body = gzip.decompress(body)
    req = ExportTraceServiceRequest()
    req.ParseFromString(body)
    for rs in req.resource_spans:
        for ss in rs.scope_spans:
            for sp in ss.spans:
                _spans.append({
                    "trace_id": sp.trace_id.hex(),
                    "name": sp.name,
                    "attributes": {kv.key: _value(kv.value) for kv in sp.attributes},
                })
    return Response(status_code=200)


@app.post("/v1/{signal}")
async def other(signal: str):
    return Response(status_code=200)


@app.get("/spans")
def get_spans():
    return _spans


@app.delete("/spans")
def clear_spans():
    _spans.clear()
    return {"spans": 0}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=4318)
