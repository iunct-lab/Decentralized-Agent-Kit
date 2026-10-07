# DAK 持続可能化システム — 運用マニュアル

このリポジトリを継続的・サステナブルに更新し続けるための自動化群の説明。
全体設計は plan file（`DAK Sustainability & Self-Improvement System`）に対応する。

## 判断エンジンの階層

| Tier | 実行者 | 用途 | コスト |
|------|--------|------|--------|
| 0 | 決定論ルール | semver 判定 + CI green/red。自動マージ可否の大半 | 無料・即時 |
| 1 | Ollama 小型 / DAK 自エージェント | changelog リスク・機能差分の一次判定、nightly eval、golden 生成 | 安価（ドッグフード） |
| 2 | `anthropic/claude-code-action` | major/破壊的疑いの精査、機能取り込み提案、新技術ウォッチ、憲章レビュー | 従量 |

## ワークフロー一覧

| ファイル | トリガ | 役割 | Tier |
|----------|--------|------|------|
| `ci.yml` | PR / push(main) | unit マトリクス + fake-LLM 統合（既存）+ 依存のライセンスの確認（`license`。`scripts/license_check.sh`、方針は [license-policy.md](license-policy.md)） | — |
| `labels.yml` | `labels.yml` 変更 / 手動 | ラベル体系を宣言的に同期 | — |
| `project-autoadd.yml` | Issue/PR open | 新規 Issue/PR を Project に自動追加し、新しい Issue の Status を Backlog にする | — |
| `dependency-triage.yml` | `pull_request_target`(dependabot) | 依存PRを判定し auto-merge or レビュー要求 | 0→1 |
| `feature-sync.yml` | weekly cron | 依存の新機能を要約し、取り込みの要望 Issue を起票 | LLM(中立) |
| `tech-watch.yml` | 隔週 cron | 憲章に沿う新技術を探索し、要望 Issue を起票 | LLM(中立)+検索 |
| `charter-review.yml` | 四半期 cron | 憲章の見直しの要望 Issue を起票 | LLM(中立)+検索 |
| `nightly-eval.yml` | nightly cron | 小型 Ollama で実LLMスモークを実行し pass-rate 記録 | 1 |
| `capture-golden.yml` | 手動 / nightly | 実LLMセッションを決定論テスト化して PR 提案 | 1 |

定期実行の提案は `scripts/setup/request_issue.py` が「要望: 」の題・`type:request` ラベルで起票し、Project に Status=Backlog で載せる（`GITHUB_TOKEN` で作った Issue は `project-autoadd` を起動しないため、スクリプトが `gh project` の CLI で自分で行う）。採否を検討して PBI にするのは人。テストは `uv run --no-project --with pytest pytest scripts -q`（CI の maintenance ジョブでも走る）。

## 初期セットアップ（一度だけ）

1. **Project 作成**: `bash scripts/setup/bootstrap_project.sh`
   → 出力された URL を登録:
   - `gh variable set DAK_PROJECT_URL --body "<URL>"`
   - `gh secret set DAK_PROJECT_TOKEN --body "<project スコープ付き PAT>"`（`project-autoadd` と `scripts/setup/request_issue.py` が user-level Project に書くため、既定 `GITHUB_TOKEN` では不可）
   - Project の設定で、Status の選択肢に `Backlog` を足す（新しい Project の既定は Todo / In Progress / Done だけ。新しい Issue と定期実行の要望は Status=Backlog で載るので、無いと失敗する）
2. **バックログ投入**: `bash scripts/setup/seed_backlog.sh`（Phase 2-4 を Issue 化）
3. **ラベル同期**: `labels.yml` を main に push（`labels.yml` ワークフローが反映）
4. **リポジトリ設定**:
   - Settings → General → Pull Requests → **Allow auto-merge** を ON
   - Settings → Branches → `main` の branch protection で **CI を必須チェック** に
5. **LLM プロバイダ設定（provider 中立・実行時選択）**: reasoning 系ワークフロー
   （tech-watch / feature-sync / charter-review）と triage の LLM リスク評価は、以下の
   env で任意のプロバイダを選ぶ（Gemini / Ollama / OpenAI / Anthropic すべて OpenAI 互換で叩ける）:
   - `gh variable set MAINT_LLM_BASE_URL --body "<base url>"`
   - `gh variable set MAINT_LLM_MODEL --body "<model id>"`
   - `gh secret set MAINT_LLM_API_KEY --body "<api key>"`（Ollama は任意の値でOK）
   - プリセット例（in/out per 1M tok、2026-10-01 確認。候補の比較・無料枠の条件・月額の見積もり・切り替えと元に戻す手順は [model-choice.md](model-choice.md)）:
     - **OpenAI**: `https://api.openai.com/v1` / `gpt-6-luna`（$0.10/$0.50）。`gpt-5.6-luna` $0.20/$1.20、`gpt-5-nano` $0.05/$0.40
     - **Gemini**: `https://generativelanguage.googleapis.com/v1beta/openai` / `gemini-3.5-flash-lite`（$0.30/$2.50、AI Studio 無料枠あり。無料枠の入出力は Google の製品改善に使われうる）。`gemini-3.5-flash` $1.50/$9.00、`gemini-3.8-flash` $0.75/$3.75（2026-12-31 まで。2027-01-01 から $1.50/$7.50）
     - **Ollama**: `http://<host>:11434/v1` / `llama3.1:8b`（無料・要ホスト）
     - **Amazon Bedrock（IAM。API キー不要）**: `MAINT_LLM_MODEL=bedrock/global.openai.gpt-6-luna`（inference profile。`MAINT_LLM_BASE_URL` / `MAINT_LLM_API_KEY` は使わない）。ワークフローが GitHub の OIDC で IAM ロールを引き受け、Converse API を呼ぶ。準備:
       - AWS に GitHub の OIDC プロバイダ（`token.actions.githubusercontent.com`、audience `sts.amazonaws.com`）と、`repo:<owner>/<repo>:ref:refs/heads/main` だけが引き受けられるロールを作る。権限は使う inference profile とその基になるモデルへの `bedrock:InvokeModel` だけ
       - `gh variable set MAINT_AWS_ROLE_ARN --body "<ロールの ARN>"`（リージョンを変えるなら `MAINT_AWS_REGION`。既定 `us-east-1`）
       - Bedrock に切り替えたら `MAINT_LLM_BASE_URL` を消す（`dependency-triage` は BASE_URL があるときだけ LLM で判定する。Dependabot の PR は main ではないのでロールを引き受けられず、ヒューリスティックの判定のまま動く）
       - `vars.MAINT_ASSESSOR=llm` を明示しているなら外す（明示すると triage が Bedrock を認証情報なしで呼び、毎回失敗してヒューリスティックに落ちる）
       - AWS の認証情報は、LLM を呼ぶ手順にだけ渡す（依存の導入や Issue の起票の手順には渡さない）。応答が途中で切れた・空のときは失敗として扱う（「提案 0 件」には見せない）
   - **triage で LLM 評価も使う場合**は上記を **Dependabot secrets/variables にも登録**
     （Dependabot PR には通常の Actions secrets が渡らないため）。未設定なら triage は
     heuristic（キーワード）評価にフォールバックし、reasoning 系は提案 0 件で失敗しない。
   - **Web 検索は Tavily を使用**: `gh secret set TAVILY_API_KEY`（+ Dependabot は不要）。
     `tech-watch` / `charter-review` は Web 検索が必須なので、未設定だと提案 0 件になる
     （`feature-sync` は changelog ベースなので Tavily 不要）。
     ※ 規約遵守のため DuckDuckGo 等の HTML スクレイピングは行わない。

## エスカレーション経路（依存更新）

```
Dependabot PR
  └─ CI (ci.yml) 完了
       └─ dependency-triage.yml
            ├─ Tier0: semver 判定 + CI 結果
            │    ├─ patch/minor + green + 非破壊 → auto-merge-candidate → gh pr merge --auto
            │    └─ それ以外 → 次へ
            └─ Tier2(必要時): changelog/CVE 精査
                 └─ needs-human-review ラベル + リスク要約コメント + Project 追跡
```

## 注意・既知の制約

- **Dependabot と secrets**: 上記のとおり両方のストアに登録が必要。
- **project-autoadd と Dependabot**: 以前 `project-autoadd.yml` は `pull_request`（`_target`
  ではない）で動いており、Dependabot が起点のイベントは Dependabot secrets ストアからしか
  secrets が供給されないため、通常の Actions secrets である `DAK_PROJECT_TOKEN` が空になって
  必ず失敗していた。このジョブは PR コードの checkout を一切行わない（GitHub API 呼び出しのみ）
  ため `pull_request_target` に切り替えても信頼できないコード実行のリスクがなく、常に通常の
  Actions secrets が使われるようになった（`dependency-triage.yml` と同じ設計）。
- **Ollama on CPU は遅い**: nightly 限定・小型モデル・寛容アサーション・モデル cache。PR ゲートには載せない。
- **提案ノイズ**: `tech-watch`/`feature-sync` は 1 実行あたり件数上限と重複検出を持たせる。
