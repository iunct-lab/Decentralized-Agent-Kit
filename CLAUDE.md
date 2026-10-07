# CLAUDE.md

Claude Code 向けのリポジトリ・ガイド。エージェント全般の設計思想は `AGENTS.md` を、
目的・スコープの判断軸は `docs/CHARTER.md` を参照（このファイルはそれらを補完する）。

## アーキテクチャ地図（4 サービス + テスト）

独立コンテナの疎結合モノレポ（各ディレクトリが独自 `pyproject.toml`/`uv.lock`）:

| dir | 役割 | ポート(host) |
|-----|------|------|
| `agent/` | google-adk ベースのコアエージェント（`dak_agent/`）。A2A + MCP + adaptive mode + コンテキストハーネス（`harness.py`, `docs/architecture/harness_engineering.md`） | 8000 |
| `mcp-server/` | FastMCP ツールサーバ（`main.py`, `policy.py` の安全層） | 8001 |
| `bff/` | HTMX 用 Backend-for-Frontend（FastAPI） | 8002 |
| `cli/` | `dak-cli`（Typer/Rich） | — |
| `tests/integration/` | Docker 横断の統合/E2E（fake-LLM, Playwright, 実LLMスモーク） | — |
| `maintenance/` | 自己保守ツールキット（依存トリアージ）。`docs/maintenance/README.md` | — |

## テスト 3 層（速い順）

```bash
# 1. コンポーネント単体（速い・Docker不要）
cd agent && uv sync && uv run pytest -q     # cli / bff / mcp-server / maintenance も同様

# 2. 統合（fake-LLM, APIキー不要, 決定論）
docker compose -f docker-compose.yml -f docker-compose.test.yml up -d --build --wait
cd tests/integration && uv sync && uv run pytest -q
docker compose -f docker-compose.yml -f docker-compose.test.yml down -v

# 3. 実LLMスモーク（Ollama, ゲート付き）
./scripts/smoke_local_llm.sh            # DAK_SMOKE_REAL_LLM=1 で有効化
```

fake-LLM はモデル名ごとに応答をスクリプトできる制御API（`/script/{model}`）を持つ。
これが決定論テストとゴールデン再生（`tests/integration/golden/`）の継ぎ目。

## 持続可能化システム（このリポジトリの自己保守）

`docs/maintenance/README.md` が全体像。要点:

- **依存更新**: Dependabot → `dependency-triage` が semver+CI+リスクで判定し安全なら自動マージ。
  判定ロジックは `maintenance/dak_maintenance`（CLI `dak-maint triage`）。同じロジックは
  DAK スキル `agent/skills/dependency_maintenance/` からエージェントも実行できる。
- **新技術ウォッチ**: `tech-watch` / `charter-review` が `docs/CHARTER.md` を軸に提案 Issue を起票。
- **継続テスト**: `nightly-eval`（小型 Ollama）+ golden replay の自動増殖。`docs/eval/`。
- **すべての作業は GitHub Project 管理**（DAK の Project の固有の値は root の `ops.config.json`）。

## 保守・検証の入口

- 新技術ウォッチ・依存の新機能・憲章見直しは GitHub Actions が定期実行する（`tech-watch` / `feature-sync` / `charter-review`）。今すぐ回すときは `gh workflow run <name>.yml`。依存更新の判定は Dependabot の PR ごとに `dependency-triage` が走る。
- ロジックは `maintenance/`（CLI `dak-maint`、`maintenance/README.md`）、判断基準は `docs/CHARTER.md`。手元で `dak-maint` の `watch` / `feature-sync` / `charter-review` を回すには `MAINT_LLM_*`（`watch` と `charter-review` は `TAVILY_API_KEY` も）が要る。`triage` は無くても動く。
- テストは上の「テスト 3 層」。
- ランタイムの DAK スキル（`agent/skills/`）の作り方は `agent/skills/README.md`。

コーディングエージェント向けの共通スキル・規約・スラッシュコマンドは、このリポジトリに置かない（ユーザーレベルで提供される）。
このファイルと `AGENTS.md` には DAK 固有のことだけを書く。

## 規約

- 「System ENABLES, Agent DECIDES」— 暗黙の副作用（特に決済）を足さない。
- 独立コンテナ/疎結合を壊さない（他コンポーネントの内部実装に依存しない）。
- 鍵の値をファイル（スクリプト・テスト・ドキュメント）に書かない。実行中のコンテナや `.env` から値をコピーせず、環境変数から読む。
  クローンしたら `scripts/setup/install_hooks.sh` で pre-commit と pre-push（どちらも gitleaks）を有効にする。CI の `secrets` ジョブも全履歴を走査する。
  標準で検出されない鍵の形式は `.gitleaks.toml` にルールを足す。検出を `.gitleaksignore` で黙らせるのは、失効を確認した鍵だけ。
- push 前・PR レビューでは、`docs/security/review-checklist.md` の「LLM が見る項目」を確かめる。検査の入口は `scripts/security/check.sh`（フック・gitleaks・範囲をまとめて確かめ、項目の一覧も出す）。
- 変更にはテストを添える。コミットは Conventional Commits 風（`feat:`/`fix:`/`test:`/`deps:`）。
