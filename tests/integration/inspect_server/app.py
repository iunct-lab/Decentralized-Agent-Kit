"""A caller's inspection endpoint for the integration tests (`dak:inspection`'s
`http`). Tests script its verdicts; each POST /validate answers the next one
({"valid": true} when none is left) and is logged for GET /requests."""
from collections import deque
from typing import Any, Dict, List

import uvicorn
from fastapi import FastAPI, Request
from pydantic import BaseModel

app = FastAPI(title="Inspect Server")

_verdicts: deque = deque()
_requests_log: List[Any] = []


class Script(BaseModel):
    verdicts: List[Dict[str, Any]]


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/script")
def set_script(script: Script):
    _verdicts.extend(script.verdicts)
    return {"queued": len(_verdicts)}


@app.delete("/script")
def clear_script():
    _verdicts.clear()
    _requests_log.clear()
    return {"queued": 0}


@app.get("/requests")
def get_requests():
    return _requests_log


@app.post("/validate")
async def validate(request: Request):
    _requests_log.append(await request.json())
    return _verdicts.popleft() if _verdicts else {"valid": True, "errors": []}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
