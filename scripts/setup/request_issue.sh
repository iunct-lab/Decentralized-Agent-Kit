#!/usr/bin/env bash
# request_issue.sh "<title>" <body-file> — 要望 Issue（type:request）を作り Status Backlog — 共通キット（Switchboard の ops-kit）の入口。設定は root の ops.config.json。キットは OPS_KIT、無ければ SWITCHBOARD_REPO_ROOT/ops-kit
R="$(cd "$(dirname "$0")/../.." && pwd)"
K="${OPS_KIT:-${SWITCHBOARD_REPO_ROOT:+$SWITCHBOARD_REPO_ROOT/ops-kit}}"
[[ -n "$K" && -f "$K/src/cli.ts" ]] || { echo "共通キットが見つかりません。Switchboard のチェックアウトの ops-kit を OPS_KIT=<path> で渡してください" >&2; exit 2; }
exec bun "$K/src/cli.ts" request --config "$R/ops.config.json" "$@"
