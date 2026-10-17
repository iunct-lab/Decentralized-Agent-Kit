# DAK の Project の固有の値

DAK の作業は GitHub Project で管理する。この文書は、その Project の DAK 固有の値が何を意味するかだけを書く。
値そのもの（URL、選択肢の並び）の正は root の [`ops.config.json`](../../ops.config.json) で、ここには転記しない。

## `ops.config.json`

| キー | 意味 |
|---|---|
| `projectUrl` | DAK の Project の URL。Actions の Variables の `DAK_PROJECT_URL` はここから登録する（手順は [README.md](README.md) の初期セットアップ）。Project を移したら両方を直す |
| `fields.area` | Project の Area フィールドの選択肢（下の「Area」） |
| `fields.phase` | Project の Phase フィールドの選択肢（下の「Phase と Milestone」） |

ここに無いフィールド（Status、Kind、Priority、Story Points）は、DAK 固有の値を持たない。

## Area

Area は、変更が主にどのコンポーネントに入るかを表す。

| 値 | 指すもの |
|---|---|
| `agent` | `agent/`（コアエージェント） |
| `mcp` | `mcp-server/`（ツールサーバ） |
| `bff` | `bff/`（HTMX 用 BFF） |
| `cli` | `cli/`（`dak-cli`） |
| `infra` | 上のどれでもないもの（`docker-compose*.yml`、`.github/`、`scripts/`、`maintenance/`、`tests/integration/`、`docs/` など） |

`.github/labels.yml` の `area:*` ラベルは、この選択肢のどれかと同じ名前にする（`scripts/setup/test_ops_config.py` が確かめる）。

## Phase と Milestone

Phase はロードマップの段階を表す。`Phase 0` は土台（段階に入る前の整備）。
Milestone を作る段階は `Phase N — <その段階の題>` の名前にし、Project の Phase と同じ `N` を使う。

## Status

Status の選択肢には `Backlog` が要る。要望は共通キットの入口 `scripts/setup/request_issue.sh` が Status=Backlog で載せるので、無いと失敗する（以前の `project-autoadd` と `request_issue.py` は #822 でやめた）（新しい Project の既定は Todo / In Progress / Done だけ）。

## トークン

`DAK_PROJECT_TOKEN`（Actions の Secrets）は、以前 `project-autoadd` と定期実行の起票が Project に書くために使った。#822 でそれらをやめ、secret も消した。Project への書き込みは共通キットの入口が利用者の認証で行う。
