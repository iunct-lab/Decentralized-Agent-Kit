#!/usr/bin/env bash
# 1 つのコンポーネントの実行時の依存（dev を除く）のライセンスを、方針（maintenance/license-policy.toml）と照らす。
# 方針と取り方は docs/maintenance/license-policy.md（PBI #344）。CI の license ジョブも同じものを回す。
#
# Usage:
#   ./scripts/license_check.sh <agent|mcp-server|bff|cli|maintenance>
#   LICENSE_REPORT_DIR=/path ./scripts/license_check.sh cli   # 一覧の出力先（既定 ./license-report）
#
# 許容外か不明の依存があれば、その行を出して終了コード 1。
# コンポーネントの .venv を dev なしで入れ直すので、終わったら `cd <component> && uv sync` で dev を戻す。

set -euo pipefail
cd "$(dirname "$0")/.."

component=${1:-}
case "$component" in
  agent|mcp-server|bff|cli|maintenance) ;;
  *) echo "usage: $0 <agent|mcp-server|bff|cli|maintenance>" >&2; exit 2 ;;
esac

report_dir=${LICENSE_REPORT_DIR:-license-report}
mkdir -p "$report_dir"
report_dir=$(cd "$report_dir" && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

self=$(sed -n 's/^name = "\(.*\)"$/\1/p' "$component/pyproject.toml" | head -1)

(
  cd "$component"
  uv sync --frozen --no-dev -q
  # pip-licenses は別に動かし、--python でコンポーネントの環境を読む（自分の依存を一覧に混ぜない）
  uvx -q pip-licenses==5.5.5 --python .venv/bin/python --with-system --from=all --format=json --with-urls > "$work/licenses.json"
  # 環境に入らない依存（Windows でだけ入るものなど）は lock から拾い、PyPI の JSON で判定する
  uv export --frozen --no-dev --format requirements-txt -q > "$work/lock.txt"
)

cd maintenance
uv run --frozen -q dak-maint license-check --component "$component" --input "$work/licenses.json" \
  --lock "$work/lock.txt" --exclude "$self" --policy license-policy.toml \
  --markdown-out "$report_dir/$component.md"
