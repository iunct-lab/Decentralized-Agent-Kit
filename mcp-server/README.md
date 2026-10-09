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
| `SANDBOX_MODE` | `off` | `off`: no isolation, every tool uses the shared `/projects` as before. `inproc`: a temporary directory per session; file-tool paths outside it are refused. Not a security boundary: `run_command` starts there but can reach anything. `docker`: a disposable container per session; `run_command` and the file tools run inside it (`docker exec`). An unknown value stops the server. |
| `SANDBOX_IMAGE` | `python:3.12-slim` | Image of the `docker` mode containers |
| `SANDBOX_TTL_SECONDS` | `900` | A session unused this long is destroyed; all are destroyed when the server stops |
| `SANDBOX_CPUS` | `1` | `docker run --cpus` |
| `SANDBOX_MEMORY` | `512m` | `docker run --memory` |
| `SANDBOX_PIDS_LIMIT` | `128` | `docker run --pids-limit` |

`docker` mode needs a Docker daemon the server can reach. `docker-compose.yml`
does not mount the host's Docker socket into this container: whoever controls
that socket controls the host. The opt-in override mounts it together with
`SANDBOX_MODE=docker`:

```bash
docker compose -f docker-compose.yml -f docker-compose.sandbox.yml up -d --build
# rootless Docker: DAK_DOCKER_SOCKET=$XDG_RUNTIME_DIR/docker.sock docker compose ...
```

In a container that sees `/var/run/docker.sock`, the server refuses to start
unless `SANDBOX_MODE=docker` (`run_command` would otherwise reach the host's
daemon unisolated).

### Command sandbox (`MCP_COMMAND_SANDBOX`, optional)

`run_command` can run each command inside
[anthropics/sandbox-runtime](https://github.com/anthropics/sandbox-runtime)
(`srt`, bubblewrap on Linux): the command can write only to `/projects` and
`/tmp`, cannot read the denied paths, and reaches only the allowed network
destinations (none by default). Design, measurements and the options that were
weighed: [`docs/design/command-sandbox.md`](../docs/design/command-sandbox.md).

```bash
DAK_UID=$(id -u) DAK_GID=$(id -g) \
  docker compose -f docker-compose.yml -f docker-compose.command-sandbox.yml up -d --build
```

| Variable | Default | Meaning |
|---|---|---|
| `MCP_COMMAND_SANDBOX` | `off` | `off`: commands run as before. `srt`: `srt --settings <MCP_SRT_SETTINGS> -c <command>`, starting in `/projects` (`/app` is read-only inside srt). If srt or the settings file is missing, or `SANDBOX_MODE=docker` is set too, the command is **not run** and an error is returned. Any other value is an error too, so a typo never drops the sandbox. |
| `MCP_SRT_SETTINGS` | `/app/srt-settings.json` | srt's settings file |
| `DAK_UID` / `DAK_GID` | `1000` | The user the server runs as in `docker-compose.command-sandbox.yml`. Use yours so the server can write the files mounted at `/projects`. With rootless Docker a container uid other than 0 maps to another host uid and cannot write your files. |

`srt-settings.json` (to change it, mount your own copy and point `MCP_SRT_SETTINGS` at it):

| Key | Default | Meaning |
|---|---|---|
| `filesystem.allowWrite` | `["/projects", "/tmp"]` | The only writable paths; everything else is read-only |
| `filesystem.denyRead` | `["/projects/.env", "/root"]` | Hidden: a file is replaced by `/dev/null` (reads as empty, or is refused where `/projects` is mounted `nodev` as with rootless Docker), a directory by an empty one. `/projects/.env` is the `.env` with the API keys |
| `filesystem.denyWrite` | `[]` | Read-only paths inside `allowWrite` |
| `network.allowedDomains` | `[]` | Destinations a command may reach through srt's proxy. Empty: no network. To let commands call the agent: `["agent:8000"]` |
| `network.deniedDomains` | `[]` | Destinations refused even when allowed above |
| `network.allowLocalBinding` | `false` | Whether a command may listen on a local port |

**What the override loosens.** bubblewrap needs namespaces, which Docker's
defaults forbid, so `docker-compose.command-sandbox.yml` lowers the
mcp-server container's own protection below Docker's defaults:

- `seccomp=./mcp-server/seccomp-srt.json`: Docker's default seccomp profile
  (moby/profiles `seccomp/default.json` at `6fe7deb1b9fb`, Apache-2.0) with 13
  namespace and mount syscalls allowed without `CAP_SYS_ADMIN`. The rest of the
  default profile stays.
- `systempaths=unconfined`: the container's `/proc` is no longer masked or
  read-only, also for the file tools, so that srt can mount a fresh `/proc` for
  each command. The server runs as non-root, which keeps `/proc/kcore`,
  `/proc/sys` and the like behind the kernel's file permissions. Do not run it
  as root with this override.

**Why it is off by default.** It needs the loosened container above, adds
about 450 MB to the image (Node.js and srt) and about 0.5 s to each command,
and srt is a beta research preview.

**What it does not cover.** The file tools (`read_file`, `write_file`, ...)
run in the server process, outside srt (protected paths: #109; per-session
isolation: `SANDBOX_MODE` above). Commands still inherit the server's
environment variables.

**Where it was tried.** Linux arm64 (kernel 6.18) with rootless Docker 29.8.1
(the design doc). `tests/integration/test_command_sandbox.py` checks the
override's stack; it needs only mcp-server, so it also runs against that service
started alone:

```bash
docker compose -f docker-compose.yml -f docker-compose.test.yml -f docker-compose.command-sandbox.yml \
  up -d --build --wait --no-deps mcp-server
cd tests/integration && uv run pytest test_command_sandbox.py -q
```

On that host the 5 tests pass with a checkout every directory of which uid 1000
can write (rootless Docker maps the container uid to another host uid): srt
puts empty placeholders over protected paths that do not exist yet (such as
`.claude/commands`) and removes them when the command ends, so the server's
uid must be able to write the directories under `/projects`, not only
`/projects` itself. Rootful Docker, Docker Desktop, hosts with AppArmor (Ubuntu 24.04 and later
restrict unprivileged user namespaces by default) and x86_64 are untested.
The host kernel must be Linux 5.2 or later: on 4.14, bubblewrap 0.12 stops with
`Can't open source /: Function not implemented` (no `open_tree`), and every
command is refused.

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
