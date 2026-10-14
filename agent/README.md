# DAK Agent

The main agent service for the Decentralized Agent Kit.

## Overview

The DAK Agent is a FastAPI-based service that provides an intelligent agent powered by Large Language Models (LLMs). It supports multiple LLM providers, integrates with the Model Context Protocol (MCP) for tool usage, and implements Agent-to-Agent (A2A) communication.

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│                      DAK Agent                           │
│                    (google-adk)                          │
├──────────────────────────────────────────────────────────┤
│                                                          │
│  ┌────────────┐  ┌─────────────┐  ┌─────────────────┐    │
│  │ ADK Web    │  │ LLM Manager │  │  MCP Client     │    │
│  │            │  │             │  │                 │    │
│  │ • /run     │  │ • Gemini    │  │ • Tool Discovery│    │
│  │ • /apps/*  │  │ • OpenAI    │  │ • Tool Execute  │    │
│  │            │  │ • Anthropic │  │                 │    │
│  └────────────┘  └─────────────┘  └─────────────────┘    │
│         │               │                    │           │
│         └───────────────┴────────────────────┘           │
│                         │                                │
│              ┌──────────▼──────────┐                     │
│              │   Agent Core        │                     │
│              │  • State Manager    │                     │
│              │  • Chat History     │                     │
│              │  • A2A Handler      │                     │
│              └─────────────────────┘                     │
└──────────────────────────────────────────────────────────┘
```

## Features

### LLM Integration

Supports multiple LLM providers with automatic tool calling:

- **Gemini**: Google's Gemini models (`gemini-...`; the built-in default and its notes are in `.env.example`)
- **OpenAI**: `openai/<model>`
- **Anthropic**: `anthropic/<model>`

The provider is selected by the LiteLLM-format `MODEL_NAME`; configure via environment variables:
```bash
MODEL_NAME=<litellm-model>  # optional: 'gemini-...', 'openai/...', 'anthropic/...', 'bedrock/...', 'ollama_chat/...'
GOOGLE_API_KEY=your_key
OPENAI_API_KEY=your_key  # if using OpenAI
ANTHROPIC_API_KEY=your_key  # if using Anthropic
```

### MCP Client Integration

The agent dynamically discovers and executes tools from the MCP Server:

1. **Tool Discovery**: Lists available tools at runtime
2. **Schema Conversion**: Converts MCP tool schemas to LLM function schemas
3. **Tool Execution**: Executes tools via MCP protocol
4. **Result Integration**: Feeds tool results back to LLM

See [MCP Integration Guide](../docs/guides/mcp_integration.md) for details.

### State Management

Conversation history is persisted using:

- **PostgreSQL** (default): Uses PostgreSQL for persistence via `google-adk`
- **InMemory** (fallback): In-memory storage if database unavailable

State is keyed by `(user_id, session_id)` for multi-user support.

### Authentication

Supports three authentication modes:

1. **JWT**: Validates NextAuth.js tokens from UI
2. **Header-based**: Uses `X-User-ID` and `X-Session-ID` headers
3. **Debug**: Falls back to default user/session for development

### A2A Protocol

Agent-to-Agent communication endpoints (A2A 1.0 over JSON-RPC; 0.3 peers are still accepted):

- `GET /a2a/dak_agent/.well-known/agent-card.json`: Agent Card (written by `entrypoint.sh`; `AGENT_NAME` sets the card's `name`, `AGENT_PUBLIC_URL` the endpoint it advertises; `skills` lists the skills loaded from `AGENT_SKILLS_DIRS`)
- `POST /a2a/dak_agent`: JSON-RPC (`SendMessage`, `SendStreamingMessage`, `GetTask`, … and the 0.3 `message/send` / `message/stream`)

The path is the ADK app's directory (`dak_agent`), not `AGENT_NAME`. The gap to the A2A specification and the version policy: [docs/architecture/a2a.md](../docs/architecture/a2a.md).

Card signatures (off by default): with `AGENT_CARD_SIGNING_KEY_FILE` (an EC P-256 private key PEM file) set, the card is served with one ES256 signature (`kid` = `AGENT_CARD_SIGNING_KID`).
A consumer checks a peer's card with the public key PEM file in that peer's `a2a_peers[].card_public_key_file`, and puts the result (`verified` / `unsigned` / `invalid` / `unchecked`) first in the sub-agent's description; no result drops the peer.
A consumer also adds the peer card's description and skills to the sub-agent's description (cut at 1024 characters), so `a2a_peers[].capabilities` is optional.
Key commands and the four states: [docs/architecture/a2a.md](../docs/architecture/a2a.md) §7.

## API Endpoints

### Main endpoints

#### `POST /run`

Run the agent with a prompt.

**Request**:
```json
{
  "app_name": "dak_agent",
  "user_id": "user123",
  "session_id": "session_abc",
  "new_message": {
    "parts": [{"text": "Use deep_think to analyze consciousness"}]
  }
}
```

**Response**:
```json
[
  {
    "content": {
      "role": "model",
      "parts": [{"text": "Based on the DeepThink analysis..."}]
    }
  }
]
```

#### `GET /capabilities`

Get agent capabilities.

**Response**:
```json
{
  "name": "dak-agent",
  "capabilities": ["chat", "tool_use", "a2a"]
}
```

## Configuration

### Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MODEL_NAME` | No | `DEFAULT_MODEL_NAME` in `dak_agent/config.py` | LiteLLM model name; its prefix selects the provider. Switching models is an env change, never a code change |
| `GOOGLE_API_KEY` | Yes* | - | Google Gemini API key |
| `OPENAI_API_KEY` | Yes* | - | OpenAI API key |
| `ANTHROPIC_API_KEY` | Yes* | - | Anthropic API key |
| `SESSION_SERVICE_URI` | No | `postgresql://...` | PostgreSQL connection string |
| `MCP_SERVER_URL` | No | `http://mcp-server:8000/mcp` | MCP Server URL |
| `NEXTAUTH_SECRET` | No | - | NextAuth.js secret for JWT validation |
| `LANGFUSE_PUBLIC_KEY` | No | - | Langfuse public key for monitoring |
| `LANGFUSE_SECRET_KEY` | No | - | Langfuse secret key for monitoring |
| `LANGFUSE_BASE_URL` | No | `https://cloud.langfuse.com` | Langfuse API endpoint |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | No | - | OTLP/HTTP endpoint the GenAI spans are sent to (any OTel backend). Unset = nothing is sent |
| `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` | No | - | `SPAN_ONLY` puts the tool call's arguments on its span (`gen_ai.tool.call.arguments`). Unset = no content |
| `AGENT_CARD_SIGNING_KEY_FILE` | No | - | Path of the PEM private key (EC P-256) the Agent Card is signed with. Unset = not signed |
| `AGENT_CARD_SIGNING_KID` | No | `AGENT_NAME` | `kid` of the card's signature |

*At least one LLM provider API key is required

### Tracing (OpenTelemetry)

LLM calls and tool executions are traced as spans of the OpenTelemetry GenAI conventions (`gen_ai.operation.name` = `invoke_agent` / `chat` or `generate_content` / `execute_tool`, `gen_ai.request.model`, `gen_ai.tool.name`). A tool call's span shares its trace ID with the MCP server's log line.

**Any OTLP backend** (Phoenix, an OTel Collector, Jaeger, ...): set the standard OTel variables; nothing DAK-specific.

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
```

Without `OTEL_EXPORTER_OTLP_ENDPOINT` nothing is sent. An endpoint that cannot be reached does not fail a run.

**Message content is off by default.** Prompts, replies and tool arguments are not put on the spans. `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=SPAN_ONLY` adds the tool call's arguments to the `execute_tool` span as `gen_ai.tool.call.arguments`; they are sent as they are (no redaction), so opt in only for a backend you trust.

**With Langfuse** ([langfuse.com](https://langfuse.com)): set the keys below and the same spans also reach `<LANGFUSE_BASE_URL>/api/public/otel`, in addition to the OTLP endpoint if you set one. Both share one tracer provider, so the content setting applies to both: with Langfuse keys set, Langfuse's own view (OpenInference attributes, with prompts and replies) is kept as it was.

1. Create a free account at [cloud.langfuse.com](https://cloud.langfuse.com)
2. Get your public and secret keys from project settings
3. Set environment variables:
   ```bash
   LANGFUSE_PUBLIC_KEY=pk-lf-...
   LANGFUSE_SECRET_KEY=sk-lf-...
   LANGFUSE_BASE_URL=https://cloud.langfuse.com  # EU region (default)
   # or https://us.cloud.langfuse.com for US region
   ```
4. Restart the agent - traces will appear in your Langfuse dashboard

**Note**: Both are completely optional. The agent works normally without them.

### Dependencies

Managed via `uv` and defined in `pyproject.toml`:

- `fastapi`: Web framework
- `uvicorn`: ASGI server
- `google-generativeai`: Gemini integration
- `mcp`: MCP protocol client
- `pymongo`: MongoDB/FerretDB client
- `pyjwt`: JWT validation

## Development

### Local Setup

```bash
cd agent
uv sync
export GOOGLE_API_KEY=your_key
export PYTHONPATH=$PYTHONPATH:$(pwd)/src
uv run uvicorn src.main:app --reload
```

### Running with Docker

```bash
docker compose up --build -d agent
docker compose logs agent -f
```

### Testing

```bash
# Test agent endpoint
curl -X POST http://localhost:8000/api/run \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Hello"}'

# Test with MCP tool
curl -X POST http://localhost:8000/api/run \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Use deep_think to analyze AI"}'
```

## Project Structure

```
agent/
├── src/
│   ├── main.py              # FastAPI app entry point
│   ├── agent.py             # Core agent logic
│   ├── llm.py               # LLM provider implementations
│   ├── mcp_client.py        # MCP Client for tool usage
│   ├── state_manager.py     # Conversation history management
│   ├── auth.py              # JWT authentication
│   ├── a2a_handler.py       # Agent-to-Agent communication
│   └── api/
│       ├── ui.py            # UI-facing endpoints
│       └── a2a.py           # A2A protocol endpoints
├── pyproject.toml           # Project dependencies
├── Dockerfile               # Container image definition
└── README.md                # This file
```

## Logs

View agent logs:

```bash
docker compose logs agent -f
```

Expected log entries:

```
INFO:     Started server process [N]
INFO:state_manager:Initializing MongoStateManager
INFO:     Application startup complete
API: Received run request with prompt: ...
MCPClient: Connecting to http://mcp-server:8000/mcp...
MCP connected: protocol=2025-11-25 server=dak-agent-mcp/1.30.0 capabilities=experimental,prompts,resources,tools
MCPClient: Calling tool deep_think with args {...}
GeminiModel: Generating content for prompt: ...
```

`MCP connected` is logged once per MCP connection: the protocol version the agent
and the server agreed on, the server's name/version, and the capabilities the server
declared. The version is `2026-07-28` when the agent connects with
`ADK_ENABLE_MCP_MODERN_PROTOCOL=1` and the server speaks it (#629). Headers, URLs and
credentials are not logged.

## See Also

- [MCP Integration Guide](../docs/guides/mcp_integration.md)
- [Docker Deployment Guide](../docs/getting-started/docker_deployment.md)
- [CLI Usage Guide](../docs/guides/cli_usage.md)
