#!/usr/bin/env bash
# Real-LLM smoke verification against a local Ollama model (no API keys).
#
# Runs the full Docker stack with the agents pointed at the HOST's Ollama
# (Metal-accelerated on macOS), then executes the real-LLM smoke tests.
#
# Usage:
#   ./scripts/smoke_local_llm.sh                 # default model (llama3.1:8b)
#   LOCAL_OLLAMA_MODEL=mistral-nemo ./scripts/smoke_local_llm.sh
#   ./scripts/smoke_local_llm.sh --keep          # leave the stack running
#
# Default is llama3.1:8b: it reliably emits structured tool calls under a
# multi-tool prompt and has no "thinking" phase, so turns stay under the 120s
# client timeout. Qwen3.5 degrades to raw-JSON-text tool calls; qwen3:8b's
# <think> blocks can blow the per-turn timeout on modest (16GB) hardware.

set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/smoke_common.sh

MODEL="${LOCAL_OLLAMA_MODEL:-llama3.1:8b}"
unset CLOUD_MODEL_NAME  # the smoke tests take the model from whichever name is set
export LOCAL_MODEL_NAME="ollama_chat/${MODEL}"
COMPOSE=(docker compose -p "${DAK_SMOKE_COMPOSE_PROJECT:-dak-ollama-smoke}" -f docker-compose.yml -f docker-compose.test.yml -f docker-compose.local-llm.yml)
KEEP="${1:-}"

echo "==> Checking host Ollama..."
if ! curl -sf --max-time 3 http://localhost:11434/api/version > /dev/null; then
    echo "Ollama is not running on the host. Start it first (e.g. 'ollama serve' or the menu-bar app)." >&2
    exit 1
fi

if ! ollama list | awk '{print $1}' | grep -qx "${MODEL}"; then
    echo "==> Pulling ${MODEL}..."
    ollama pull "${MODEL}"
fi

echo "==> Warming up ${MODEL} (loads weights into memory)..."
curl -sf http://localhost:11434/api/generate \
    -d "{\"model\": \"${MODEL}\", \"prompt\": \"hi\", \"stream\": false}" > /dev/null

echo "==> Model: ${LOCAL_MODEL_NAME}"
run_smoke_stack "${KEEP}" "${COMPOSE[@]}"
