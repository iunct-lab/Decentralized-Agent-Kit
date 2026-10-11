#!/usr/bin/env bash
# Real-LLM smoke verification against a llama.cpp server (no API keys).
#
# Unlike smoke_local_llm.sh (which manages Ollama), this script does NOT start
# llama-server — it only verifies one is reachable, then runs the smoke suite
# against it. Start the server wherever the GPU is first (local Metal build,
# Colab VM behind an SSH forward, or a LAN GPU box behind ssh -L); recipes in
# docs/guides/local_llm_llamacpp.md.
#
# Usage:
#   ./scripts/smoke_llamacpp.sh                    # expects llama-server at :18080
#   LLAMACPP_PORT=8080 ./scripts/smoke_llamacpp.sh
#   LLAMACPP_WAIT=1200 ./scripts/smoke_llamacpp.sh # first run downloads the model
#   ./scripts/smoke_llamacpp.sh --keep             # leave the stack running

set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/smoke_common.sh
unset CLOUD_MODEL_NAME LOCAL_MODEL_NAME  # no direct-call model here: the overhead test skips

PORT="${LLAMACPP_PORT:-18080}"
export LLAMACPP_API_BASE="${LLAMACPP_API_BASE:-http://host.docker.internal:${PORT}/v1}"
# Probe the same endpoint the containers will use, translated to the host's
# point of view (host.docker.internal only resolves inside containers). This
# keeps the preflight honest when LLAMACPP_API_BASE is overridden directly.
PROBE_BASE="${LLAMACPP_API_BASE%/v1}"
PROBE_BASE="${PROBE_BASE/host.docker.internal/localhost}"
# Own Compose project: the shared helper removes volumes after a run (see
# scripts/smoke_common.sh), so the default project would delete the
# application's Postgres data.
COMPOSE=(docker compose -p "${DAK_SMOKE_COMPOSE_PROJECT:-dak-llamacpp-smoke}" -f docker-compose.yml -f docker-compose.test.yml -f docker-compose.llamacpp.yml -f docker-compose.llamacpp.test.yml)
KEEP="${1:-}"
WAIT="${LLAMACPP_WAIT:-300}"

echo "==> Waiting for llama-server at ${PROBE_BASE} (up to ${WAIT}s)..."
DEADLINE=$((SECONDS + WAIT))
until curl -sf --max-time 3 "${PROBE_BASE}/health" > /dev/null; do
    if [ "${SECONDS}" -ge "${DEADLINE}" ]; then
        cat >&2 <<EOF
No llama-server became reachable at ${PROBE_BASE} within ${WAIT}s.

Start one first (see docs/guides/local_llm_llamacpp.md), e.g.:
  * This machine (Metal/CUDA):
      llama-server -hf bartowski/Meta-Llama-3.1-8B-Instruct-GGUF:Q4_K_M \\
                   --port ${PORT} --jinja -c 8192
  * Colab GPU VM (google-colab-cli): ssh -N colab-llm   # LocalForward ${PORT}
  * LAN GPU server: ssh -N -L ${PORT}:localhost:8080 <gpu-host>
If the server is still downloading its model, re-run with a larger
LLAMACPP_WAIT (a first run fetches several GB).
EOF
        exit 1
    fi
    sleep 3
done

echo "==> Warming up (loads weights / verifies completion path)..."
curl -sf --max-time 120 "${PROBE_BASE}/v1/chat/completions" \
    -H 'Content-Type: application/json' \
    -d '{"model": "llamacpp", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 4}' > /dev/null \
    || { echo "llama-server answered /health but not /v1/chat/completions." >&2; exit 1; }

# llama-server ignores the model name in requests, so surface what is actually
# being served — otherwise a stale server or wrong tunnel silently attributes
# this run's pass/fail to the wrong model.
SERVED_MODEL=$(curl -sf --max-time 3 "${PROBE_BASE}/v1/models" \
    | sed -n 's/.*"id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)
echo "==> Serving model: ${SERVED_MODEL:-unknown} (endpoint: ${LLAMACPP_API_BASE})"

run_smoke_stack "${KEEP}" "${COMPOSE[@]}"
