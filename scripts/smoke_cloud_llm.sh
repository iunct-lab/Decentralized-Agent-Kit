#!/usr/bin/env bash
# Real-LLM smoke verification against a cheap CLOUD model (API key required).
#
# Same suite as scripts/smoke_local_llm.sh, but pointed at a hosted model —
# useful when local hardware is busy, or to sanity-check a provider before
# adopting it. A full run is on the order of a few cents with the default model.
#
# Usage:
#   ./scripts/smoke_cloud_llm.sh                                  # openai/gpt-5.6-luna
#   CLOUD_MODEL_NAME=gemini/gemini-2.5-flash-lite ./scripts/smoke_cloud_llm.sh
#   CLOUD_MODEL_NAME=bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0 \
#     ./scripts/smoke_cloud_llm.sh                                # Bedrock (see docs/getting-started/bedrock.md)
#   ./scripts/smoke_cloud_llm.sh --keep                           # leave the stack running

set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/smoke_common.sh

unset LOCAL_MODEL_NAME  # the smoke tests take the model from whichever name is set
export CLOUD_MODEL_NAME="${CLOUD_MODEL_NAME:-openai/gpt-5.6-luna}"
COMPOSE=(docker compose -p "${DAK_SMOKE_COMPOSE_PROJECT:-dak-cloud-smoke}" -f docker-compose.yml -f docker-compose.test.yml -f docker-compose.cloud-llm.yml)
KEEP="${1:-}"

[ -f .env ] || touch .env

# A credential can come from the shell env or from .env (compose reads both).
has_credential() {
    local var="$1"
    [ -n "${!var:-}" ] && return 0
    grep -Eq "^${var}=.+" .env
}

echo "==> Model: ${CLOUD_MODEL_NAME}"
case "${CLOUD_MODEL_NAME}" in
    openai/*)
        has_credential OPENAI_API_KEY || { echo "OPENAI_API_KEY is not set (env or .env)." >&2; exit 1; } ;;
    gemini/*|gemini-*)
        has_credential GOOGLE_API_KEY || { echo "GOOGLE_API_KEY is not set (env or .env)." >&2; exit 1; } ;;
    anthropic/*)
        has_credential ANTHROPIC_API_KEY || { echo "ANTHROPIC_API_KEY is not set (env or .env)." >&2; exit 1; } ;;
    bedrock/*)
        has_credential AWS_BEARER_TOKEN_BEDROCK || has_credential AWS_ACCESS_KEY_ID \
            || { echo "Neither AWS_BEARER_TOKEN_BEDROCK nor AWS_ACCESS_KEY_ID is set (env or .env)." >&2; exit 1; }
        has_credential AWS_REGION_NAME || { echo "AWS_REGION_NAME is not set (env or .env)." >&2; exit 1; } ;;
    *)
        echo "==> Unknown provider prefix; assuming credentials are already configured." ;;
esac

run_smoke_stack "${KEEP}" "${COMPOSE[@]}"
