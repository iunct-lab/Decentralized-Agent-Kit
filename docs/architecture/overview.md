# Architecture Overview

The Decentralized Agent Kit (DAK) is designed to be a modular and extensible platform for building autonomous agents. It follows the Google Agent Development Kit (ADK) principles.

## Core Components

### 1. Agent Service (`agent/`)
The backend service responsible for:
- **LLM Integration**: Communicating with Large Language Models (Gemini, etc.) via `google-adk`.
- **State Management**: Storing conversation history and user context (PostgreSQL or In-Memory).
- **A2A Protocol**: Serving A2A 1.0 (and 0.3 for older peers) over JSON-RPC at `POST /a2a/<AGENT_NAME>`, with the Agent Card at `GET /a2a/<AGENT_NAME>/.well-known/agent-card.json` (see [a2a.md](a2a.md)).
- **MCP Server**: Exposing tools and resources via the Model Context Protocol.

### 2. BFF UI Service (`bff/`)
A lightweight FastAPI + HTMX web interface for interacting with the agent
(Backend For Frontend pattern), served at http://localhost:8002.
- **Chat Interface**: Streaming chat with the agent, including a "thinking process" view of tool calls.

### 3. CLI Tool (`cli/`)
A command-line interface for developers and power users.
- **Direct Access**: Interact with the Agent API from the terminal.
- **Automation**: Scriptable commands for agent tasks.

```mermaid
graph LR
    User[User] -->|Interacts| UI["Web UI (BFF: FastAPI + HTMX)"]
    User -->|Commands| CLI[CLI Tool]
    
    UI -->|API Requests| Agent["DAK Agent (Python)"]
    CLI -->|API Requests| Agent
    
    subgraph Backend Services
        Agent -->|Tools & Resources| MCP[MCP Server]
        Agent -->|Persists State| DB[(PostgreSQL)]
    end
    
    MCP -->|Executes| Tools["Tools (deep_think, etc.)"]
```
## Infrastructure

- **Docker Compose**: Orchestrates all services (Agent, UI, Database).
- **PostgreSQL**: Provides persistence for conversation history.
