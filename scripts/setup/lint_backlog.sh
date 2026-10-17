#!/usr/bin/env bash
# lint_backlog.sh [--no-project] — open の PBI / Task / 要望の記載規約を検査（exit 0 合格 / 1 違反 / 2 検査できなかった） — 共通キット（Switchboard の ops-kit）の入口。設定は root の ops.config.json。キットは OPS_KIT、無ければ SWITCHBOARD_REPO_ROOT/ops-kit
R="$(cd "$(dirname "$0")/../.." && pwd)"
K="${OPS_KIT:-${SWITCHBOARD_REPO_ROOT:+$SWITCHBOARD_REPO_ROOT/ops-kit}}"
[[ -n "$K" && -f "$K/src/cli.ts" ]] || { echo "共通キットが見つかりません。Switchboard のチェックアウトの ops-kit を OPS_KIT=<path> で渡してください" >&2; exit 2; }
exec bun "$K/src/cli.ts" lint --config "$R/ops.config.json" "$@"
