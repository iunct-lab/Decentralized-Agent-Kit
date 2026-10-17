#!/usr/bin/env bash
# triage_issue.sh <issue> <area> <kind> <priority> <phase> [parent] [status] — Project に入れてフィールドと親を設定 — 共通キット（Switchboard の ops-kit）の入口。設定は root の ops.config.json。キットは OPS_KIT、無ければ SWITCHBOARD_REPO_ROOT/ops-kit
R="$(cd "$(dirname "$0")/../.." && pwd)"
K="${OPS_KIT:-${SWITCHBOARD_REPO_ROOT:+$SWITCHBOARD_REPO_ROOT/ops-kit}}"
[[ -n "$K" && -f "$K/src/cli.ts" ]] || { echo "共通キットが見つかりません。Switchboard のチェックアウトの ops-kit を OPS_KIT=<path> で渡してください" >&2; exit 2; }
exec bun "$K/src/cli.ts" triage --config "$R/ops.config.json" "$@"
