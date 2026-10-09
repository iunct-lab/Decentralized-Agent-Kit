# Shared tail for the real-LLM smoke runners (smoke_local_llm.sh,
# smoke_cloud_llm.sh, smoke_llamacpp.sh): boot the overlaid stack, run the
# smoke suite, tear down. Callers keep only their backend-specific preflight.
#
# Usage (after `set -euo pipefail` and cd to the repo root):
#   source scripts/smoke_common.sh
#   run_smoke_stack "${KEEP}" "${COMPOSE[@]}"

# Each smoke runner uses its own Compose project so `down -v` cannot delete the
# application's volumes. A separate project does NOT separate host ports, so the
# normal stack must be stopped first - detect that instead of failing with a bare
# "port is already allocated" after the ERR trap has torn everything down again.
check_ports_free() {
    local running
    running=$(docker compose -f docker-compose.yml ps -q 2>/dev/null | head -1)
    if [ -n "${running}" ]; then
        cat >&2 <<EOF
The normal DAK stack is running and binds the same host ports (8000/8001/8002/5432).
The smoke stack runs in its own Compose project, which isolates volumes but not ports.
Stop the application stack first:
  docker compose down
EOF
        return 1
    fi
    return 0
}

run_smoke_stack() {
    local keep="$1"
    shift
    local -a compose=("$@")

    [ -f .env ] || touch .env

    check_ports_free || return 1

    # `up --wait` leaves containers behind when it fails; without this trap a
    # failed boot would strand the overlaid stack on the integration-test
    # ports (8000/8002/...) and break the next plain test run.
    trap '"${compose[@]}" down -v' ERR

    echo "==> Starting the stack..."
    "${compose[@]}" up -d --build --wait

    trap - ERR

    echo "==> Running real-LLM smoke tests..."
    set +e
    (cd tests/integration && uv sync -q --group smoke && DAK_SMOKE_REAL_LLM=1 uv run pytest test_smoke_real_llm.py -v -rP -p no:cacheprovider)
    local result=$?
    set -e

    if [ "${keep}" != "--keep" ]; then
        echo "==> Tearing down..."
        "${compose[@]}" down -v
    else
        echo "==> Stack left running (BFF: http://localhost:8002)."
        # The stack lives in the runner's own Compose project, so a plain
        # `docker compose down` would not find it.
        echo "==> Stop it with: ${compose[*]} down -v"
    fi

    return "${result}"
}
