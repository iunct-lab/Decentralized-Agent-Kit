"""A stand-in for Tavily's remote MCP server for the integration tests
(test_web_search_flow.py). `tavily_search` has the real tool's name and its
query/max_results arguments, and answers fixed results. The Authorization
header of the last MCP request is kept for GET /last-auth."""
from typing import Optional

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

# Same DNS rebinding settings as mcp-server/main.py, for this service's name.
HOSTS = ["fake-tavily:*", "localhost:*", "127.0.0.1:*"]
mcp = FastMCP(
    "fake-tavily",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=HOSTS,
        allowed_origins=[f"{scheme}://{h}" for h in HOSTS for scheme in ("http", "https")],
    ),
)

RESULTS = [
    {"title": "Decentralized Agent Kit", "url": "https://fake-tavily.example/dak",
     "content": "DAK is an agent kit with A2A, MCP and adaptive modes."},
    {"title": "DAK skills", "url": "https://fake-tavily.example/dak-skills",
     "content": "Skills give the agent tools on demand."},
]

_last_auth: Optional[str] = None


@mcp.tool(name="tavily_search")
def tavily_search(query: str, max_results: int = 5) -> dict:
    """Search the web (fixed results)."""
    return {"query": query, "results": RESULTS[:max_results]}


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


@mcp.custom_route("/last-auth", methods=["GET"])
async def last_auth(request: Request) -> JSONResponse:
    return JSONResponse({"authorization": _last_auth})


class RecordAuth:
    """Keeps the Authorization header of each request to the MCP endpoint."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        global _last_auth
        if scope["type"] == "http" and scope["path"].startswith("/mcp"):
            value = dict(scope["headers"]).get(b"authorization")
            _last_auth = value.decode() if value is not None else None
        await self.app(scope, receive, send)


app = mcp.streamable_http_app()
app.add_middleware(RecordAuth)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
