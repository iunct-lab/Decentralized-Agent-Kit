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

- `GET /a2a/dak_agent/.well-known/agent-card.json`: Agent Card (written by `entrypoint.sh`; `AGENT_NAME` sets the card's `name`, `AGENT_PUBLIC_URL` the endpoint it advertises)
- `POST /a2a/dak_agent`: JSON-RPC (`SendMessage`, `SendStreamingMessage`, `GetTask`, … and the 0.3 `message/send` / `message/stream`)

The path is the ADK app's directory (`dak_agent`), not `AGENT_NAME`. The gap to the A2A specification and the version policy: [docs/architecture/a2a.md](../docs/architecture/a2a.md).

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

*At least one LLM provider API key is required

### Monitoring with Langfuse

The agent integrates with [Langfuse](https://langfuse.com) for observability via OpenTelemetry. All tool calls and model completions are automatically traced when configured.

**Setup**:

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

**Note**: Langfuse is completely optional. The agent works normally without it.

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
MCPClient: Calling tool deep_think with args {...}
GeminiModel: Generating content for prompt: ...
```

## See Also

- [MCP Integration Guide](../docs/guides/mcp_integration.md)
- [Docker Deployment Guide](../docs/getting-started/docker_deployment.md)
- [CLI Usage Guide](../docs/guides/cli_usage.md)
