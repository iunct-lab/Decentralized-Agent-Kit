# MCP Server

A Model Context Protocol (MCP) server that provides tools for the Decentralized Agent Kit (DAK) agent.

## Overview

This MCP server exposes tools that can be discovered and used by the DAK agent through the MCP protocol. It uses the FastMCP framework with streamable HTTP transport for production-ready scalability.

## Available Tools

### `deep_think`

A tool for deep thinking and complex reasoning.

**Parameters:**
- `thought` (string, required): The thought or topic to analyze

**Returns:**
- The input thought as-is (echo functionality)

**Example Usage:**
```python
# Via MCP Client
result = await mcp_client.call_tool("deep_think", {"thought": "What is consciousness?"})
# Returns: "What is consciousness?"
```

## Architecture

- **Framework**: FastMCP with `json_response=True` for JSON responses
- **Transport**: Streamable HTTP (recommended for production)
- **Network**: Binds to `0.0.0.0:8000` via Uvicorn for Docker network access
- **Session Management**: Stateful sessions with proper lifecycle handling

## Adding New Tools

To add a new tool to the MCP server:

1. Define the tool function with the `@mcp.tool()` decorator:

```python
@mcp.tool()
async def my_new_tool(param1: str, param2: int) -> str:
    """
    Brief description of what this tool does.
    
    Args:
        param1: Description of parameter 1
        param2: Description of parameter 2
    
    Returns:
        Description of return value
    """
    # Your tool logic here
    return f"Processed {param1} with {param2}"
```

2. Rebuild the mcp-server container:

```bash
docker compose up --build -d mcp-server
```

3. The tool will automatically be discovered by the agent's MCP client.

## Development

### Running Locally

```bash
cd mcp-server
uv sync
uv run python main.py
```

### Testing with MCP Inspector

You can test the MCP server using the MCP Inspector tool:

```bash
npx @modelcontextprotocol/inspector
```

Then connect to `http://localhost:8000/mcp`.

## Configuration

The server is configured in `main.py`:

- **Host**: `0.0.0.0` (accessible from Docker network)
- **Port**: `8000` (mapped to host port `8001` in docker-compose)
- **Transport**: `streamable-http`
- **Session Mode**: Stateful (for clean session lifecycle)

### Per-session isolation (`SANDBOX_MODE`)

The file tools and `run_command` can run in an environment of their own for each
user session, keyed by the agent's `X-DAK-Session-Key` header (calls without it
share the session `default`). Design: `docs/design/session-sandbox.md`.

| Variable | Default | Meaning |
|---|---|---|
| `SANDBOX_MODE` | `off` | `off`: no isolation, every tool uses the shared `/projects` as before. `inproc`: a temporary directory per session; file-tool paths outside it are refused. Not a security boundary: `run_command` starts there but can reach anything. `docker`: a disposable container per session; `run_command` runs inside it (file tools are not available in this mode yet). An unknown value stops the server. |
| `SANDBOX_IMAGE` | `python:3.12-slim` | Image of the `docker` mode containers |
| `SANDBOX_TTL_SECONDS` | `900` | A session unused this long is destroyed; all are destroyed when the server stops |
| `SANDBOX_CPUS` | `1` | `docker run --cpus` |
| `SANDBOX_MEMORY` | `512m` | `docker run --memory` |
| `SANDBOX_PIDS_LIMIT` | `128` | `docker run --pids-limit` |

`docker` mode needs a Docker daemon the server can reach. `docker-compose.yml`
does not mount the host's Docker socket into this container: whoever controls
that socket controls the host, so mounting it waits for an explicit decision
(PBI #20).

## Dependencies

Managed via `uv` and defined in `pyproject.toml`:

- `mcp>=1.22.0`: MCP protocol implementation
- `httpx>=0.28.1`: HTTP client
- `uvicorn>=0.30.0`: ASGI server
- `starlette>=0.37.0`: Web framework

## Logs

Monitor server logs:

```bash
docker compose logs mcp-server -f
```

Expected log output for successful tool execution:

```
INFO:     Started server process [N]
StreamableHTTP session manager started
Created new transport with session ID: <session-id>
Processing request of type CallToolRequest
INFO:     172.x.x.x:xxxxx - "POST /mcp HTTP/1.1" 200 OK
Terminating session: <session-id>
INFO:     172.x.x.x:xxxxx - "DELETE /mcp HTTP/1.1" 200 OK
```
