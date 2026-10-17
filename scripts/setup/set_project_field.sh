#!/usr/bin/env bash
# set_project_field.sh <issue> "<Field>" <value> — Project のフィールドを 1 つ設定 — 共通キット（Switchboard の ops-kit）の入口。設定は root の ops.config.json。キットは OPS_KIT、無ければ SWITCHBOARD_REPO_ROOT/ops-kit
R="$(cd "$(dirname "$0")/../.." && pwd)"
K="${OPS_KIT:-${SWITCHBOARD_REPO_ROOT:+$SWITCHBOARD_REPO_ROOT/ops-kit}}"
[[ -n "$K" && -f "$K/src/cli.ts" ]] || { echo "共通キットが見つかりません。Switchboard のチェックアウトの ops-kit を OPS_KIT=<path> で渡してください" >&2; exit 2; }
exec bun "$K/src/cli.ts" set --config "$R/ops.config.json" "$@"
