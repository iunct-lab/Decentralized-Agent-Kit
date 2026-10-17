# DAK 持続可能化システム — 運用マニュアル

このリポジトリを継続的・サステナブルに更新し続けるための自動化群の説明。
全体設計は plan file（`DAK Sustainability & Self-Improvement System`）に対応する。
定期の workflow は #822 でやめた（下の「ワークフロー一覧」）。以下の Tier とプロバイダの設定は、手元で `dak-maint` を回すときの説明として残す。

## 判断エンジンの階層

| Tier | 実行者 | 用途 | コスト |
|------|--------|------|--------|
| 0 | 決定論ルール | semver 判定 + CI green/red。自動マージ可否の大半 | 無料・即時 |
| 1 | Ollama 小型 / DAK 自エージェント | changelog リスク・機能差分の一次判定、golden 生成 | 安価（ドッグフード） |
| 2 | `anthropic/claude-code-action` | major/破壊的疑いの精査、機能取り込み提案、新技術ウォッチ、憲章レビュー | 従量 |

## ワークフロー一覧

| ファイル | トリガ | 役割 |
|----------|--------|------|
| `ci.yml` | PR / push(main) | unit マトリクス + fake-LLM 統合 + 依存のライセンスの確認（`license`。`scripts/license_check.sh`、方針は [license-policy.md](license-policy.md)） |
| `labels.yml` | `labels.yml` 変更 / 手動 | ラベル体系を宣言的に同期 |

この 2 本は Private の開発用リポジトリでは動かず（Actions が使えない）、公開側のリポジトリで同期の PR のテストとラベルの同期に使う。`ci.yml` は Switchboard の `local-ci.ts` が読む。

**#822 でやめたもの**（2026-10-17。開発用を Private にし、DAK 独自の workflow を減らす決定）: `dependency-triage.yml`・`feature-sync.yml`・`tech-watch.yml`・`charter-review.yml`・`nightly-eval.yml`・`capture-golden.yml`・`verify-anthropic.yml`・`project-autoadd.yml`、`dependabot.yml`、定期実行の起票に使った `Python 版の request_issue`、`dak-cloud-eval-bedrock` の IAM ロール。手放した情報: 依存の自動 triage とマージ（Dependabot の PR も来ない）、新技術・依存の新機能・憲章の定期の提案 Issue、nightly eval の pass rate の記録（回帰の検知は統合テストと実 LLM スモークの手動実行）。ロジックの `maintenance/`（`dak-maint`）と DAK スキル `agent/skills/dependency_maintenance/` は残り、手元で回せる（[maintenance/README.md](../../maintenance/README.md)）。新しい Issue の Project への追加と要望の起票は共通キットの入口（`scripts/setup/`）を使う。

## 初期セットアップ（一度だけ）

1. **Project**: GitHub Project にフィールド Status（`Backlog` を含む）・Area・Kind・Priority・Phase・Story Points を持たせる。各値の意味と `ops.config.json` は [issue-workflow.md](issue-workflow.md)。URL を `ops.config.json` の `projectUrl` に書き、`gh variable set DAK_PROJECT_URL --body "$(jq -r .projectUrl ops.config.json)"`（Switchboard の道具が読む）
2. **ラベル同期**: `labels.yml` を main に push（公開側の `labels.yml` ワークフローが反映）
3. **LLM プロバイダ設定（手元で `dak-maint` を回すとき。provider 中立・実行時選択）**:
   reasoning 系（`dak-maint` の watch / feature-sync / charter-review）と triage の LLM リスク評価は、以下の
   env で任意のプロバイダを選ぶ（Gemini / Ollama / OpenAI は OpenAI 互換、Anthropic は native Messages API）:
   - 環境変数 `MAINT_LLM_BASE_URL`（base url）/ `MAINT_LLM_MODEL`（model id）/ `MAINT_LLM_API_KEY`（Ollama は任意の値でOK。キーはファイルに書かず環境から渡す）
   - プリセット例（in/out per 1M tok、2026-10-01 確認。候補の比較・無料枠の条件・月額の見積もり・切り替えと元に戻す手順は [model-choice.md](model-choice.md)）:
     - **Anthropic 直接 API（Haiku 5.5）**: `MAINT_LLM_MODEL=anthropic/claude-haiku-5-5` と `MAINT_LLM_API_KEY`。`MAINT_LLM_BASE_URL` は不要。ホストでの Parameter Store 運用は [anthropic.md](../getting-started/anthropic.md)。キーをローカルファイルへ保存しない。
     - **OpenAI**: `https://api.openai.com/v1` / `gpt-6-luna`（$0.10/$0.50）。`gpt-5.6-luna` $0.20/$1.20、`gpt-5-nano` $0.05/$0.40
     - **Gemini**: `https://generativelanguage.googleapis.com/v1beta/openai` / `gemini-3.5-flash-lite`（$0.30/$2.50、AI Studio 無料枠あり。無料枠の入出力は Google の製品改善に使われうる）。`gemini-3.5-flash` $1.50/$9.00、`gemini-3.8-flash` $0.75/$3.75（2026-12-31 まで。2027-01-01 から $1.50/$7.50）
     - **Ollama**: `http://<host>:11434/v1` / `llama3.1:8b`（無料・要ホスト）
     - **Amazon Bedrock（IAM。API キー不要）**: `MAINT_LLM_MODEL=bedrock/global.openai.gpt-6-luna`（inference profile。`MAINT_LLM_BASE_URL` / `MAINT_LLM_API_KEY` は使わない）。手元の AWS 認証情報で Converse API を呼ぶ（GitHub の OIDC で引き受けていた IAM ロールは #822 で消した）。応答が途中で切れた・空のときは失敗として扱う（「提案 0 件」には見せない）
   - `MAINT_LLM_MODEL` / `MAINT_LLM_API_KEY` / `MAINT_LLM_BASE_URL` が使うモデルに足りないと、triage は heuristic（キーワード）評価にフォールバックし、reasoning 系は提案 0 件で失敗しない。ただし Anthropic のモデル（`anthropic/…`）は `MAINT_LLM_BASE_URL` が無くても API キーがあれば呼ぶので、有料の呼び出しを避けるなら `MAINT_LLM_API_KEY` を渡さない。
   - **Web 検索は Tavily を使用**: 環境変数 `TAVILY_API_KEY`。
     `watch` / `charter-review` は Web 検索が必須なので、未設定だと提案 0 件になる
     （`feature-sync` は changelog ベースなので Tavily 不要）。
     ※ 規約遵守のため DuckDuckGo 等の HTML スクレイピングは行わない。

## 注意・既知の制約

- **`uv audit`（preview）**: ロックしている版の既知の脆弱性の ID を見る（`maintenance/src/dak_maintenance/audit.py`）。
  2026-10-14 に uv 0.12.19 で
  `uv audit --frozen --preview-features audit-command,json-output --output-format json` を各ディレクトリで流した
  （`schema.version` は `preview`）。件数は `summary.vulnerabilities`（エントリ数。ID の重複を含む）:
  `agent` 114 / `mcp-server` 69 / `bff` 16 / `cli` 20 / `tests/integration` 4（どれも終了コード 1）。
  判定に読む欄は `vulnerabilities[].id` と `vulnerabilities[].dependency.{name,version}` だけ。
  **脆弱性があると終了コードは 1** なので、終了コードでなく JSON が読めるかで成否を決める
  （読めなければ `AuditResult.error`。「脆弱性 0 件」とは区別する）。preview の出力なので欄は変わりうる。
  （`dependency-triage` は #822 でやめた。以下は手元の `dak-maint triage` の説明。）
  `dak-maint triage` は `--audit-before FILE --audit-after FILE`（上の JSON の出力）を受け取ると、更新で直る ID を
  結果の `fixes_vulnerabilities`・理由・`security` ラベルに出す。自動マージの可否（`decide`）は変えない。
- **提案ノイズ**: `tech-watch`/`feature-sync` は 1 実行あたり件数上限を持ち、既存と内容が重なる提案はログに `skip:` を出して起票しない。
  提案（題と本文の先頭）と、既存の要望（`type:request`）・PBI（`type:pbi`）・そのワークフローの
  ラベルの Issue（開いた・閉じた）の題を LLM で 1 回突き合わせ、重なる提案は
  `skip: <題>（#N と重なる）` と出す。突き合わせの応答が読めないときは実行を失敗させる
  （重複を黙って起票しない）。`charter-review` は題の一致だけのまま（題に四半期が入る）。
