#!/bin/sh
# entrypoint.sh - Dynamically configures agent.json and starts ADK server

set -e

# Read environment variables with defaults
AGENT_NAME="${AGENT_NAME:-dak_agent}"
AGENT_PUBLIC_URL="${AGENT_PUBLIC_URL:-http://localhost:8000/a2a/${AGENT_NAME}}"
AGENT_DESCRIPTION="${AGENT_DESCRIPTION:-A helpful assistant powered by the Decentralized Agent Kit.}"
AGENT_VERSION="${AGENT_VERSION:-0.1.0}"

# Path to agent.json
AGENT_JSON_PATH="/app/dak_agent/agent.json"

# Generate agent.json from template with environment variables
cat > "${AGENT_JSON_PATH}" << EOF
{
    "capabilities": {},
    "defaultInputModes": [
        "text/plain"
    ],
    "defaultOutputModes": [
        "text/plain"
    ],
    "description": "${AGENT_DESCRIPTION}",
    "name": "${AGENT_NAME}",
    "protocolVersion": "0.2.6",
    "skills": [
        {
            "description": "${AGENT_DESCRIPTION} that can help with various tasks using MCP tools.",
            "id": "${AGENT_NAME}",
            "name": "model",
            "tags": [
                "llm"
            ]
        }
    ],
    "supportsAuthenticatedExtendedCard": false,
    "url": "${AGENT_PUBLIC_URL}",
    "version": "${AGENT_VERSION}"
}
EOF

echo "[entrypoint] Generated agent.json with URL: ${AGENT_PUBLIC_URL}"

# Execute the original command (uvicorn dak_agent.server:app ...)
exec "$@"
