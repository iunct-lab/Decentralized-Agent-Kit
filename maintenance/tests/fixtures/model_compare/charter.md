# DAK 憲章 (Charter)

> このファイルは Decentralized-Agent-Kit の**目的と判断軸**を定める。
> 自動化（特に `tech-watch` / `feature-sync`）は、新しいライブラリ・仕様・手法を
> 採用すべきか否かを**この憲章に照らして**判断する。四半期ごとに見直す（下記）。

## Mission

**Decentralized-Agent-Kit (DAK)** は、「A2A (Agent-to-Agent)」と「MCP (Model Context
Protocol)」を土台とする **P2P AI エージェント・エコシステム** の MVP である。
疎結合・独立コンテナ・LLM 自律性を核に、ツール/スキルを動的に発見し、エージェント同士が
通信・協調（必要なら決済）できる、モジュラなマルチ LLM エージェント基盤を提供する。

## 設計原則（不変の判断軸）

1. **System ENABLES, Agent DECIDES** — システムは能力を「可能にする」だけで、実行は
   エージェントが明示的に決める。暗黙の副作用（特に決済）を持ち込む変更は原則却下。
2. **Loosely Coupled / Independent Containers** — 各コンポーネント（agent, mcp-server,
   bff, cli）は独立コンテナとして単体で動く。標準プロトコル（MCP, HTTP/REST, A2A）のみで結合。
3. **Observation-Driven** — 障壁（例: Payment Required）は構造化された Observation として
   返し、エージェントが推論して次を決める。
4. **Multi-LLM 中立** — 特定ベンダにロックインしない（LiteLLM 経由で Gemini/OpenAI/
   Anthropic/Ollama を切替）。ローカル LLM でも成立することを重視。

## スコープ内の技術ドメイン（ウォッチ対象）

新技術は、以下のいずれかを**明確に前進させる**場合に採用候補とする:

- **エージェント基盤 / オーケストレーション** — google-adk、その周辺のプランニング/
  ツール実行ループ、adaptive/mode-switching の改善。
- **A2A（Agent-to-Agent）** — a2a-sdk、エージェント間発見・委譲・信頼の仕様。
- **MCP（Model Context Protocol）** — サーバ/クライアント仕様、ツール発見、streamable HTTP。
- **マルチ LLM 抽象** — LiteLLM、OpenAI 互換、Ollama。小型ローカルモデルでの tool-calling 品質。
- **安全性 / サンドボックス** — コマンド allow/deny、パス封じ込め、per-session 使い捨て
  コンテナ、監査ログ（`docs/comparison/claude-code-gap.md` のロードマップに対応）。
- **観測性** — OpenTelemetry / LangFuse による trace/eval。
- **決済 / 検証可能な意図** — AP2 / x402 / Solana（experimental、mock 前提を維持）。

## スコープ外（現時点）

- 単一ベンダ専用機能への依存（Multi-LLM 中立を壊すもの）。
- エージェントの明示的判断を迂回する「自動実行」系（原則1に反するもの）。
- コア MVP に無関係なアプリ機能（汎用 SaaS 化、独自 UI フレームワーク移行など）。
- 重量級の新規常時依存（小型ローカル LLM での動作を阻害するもの）。

## 新技術の採用基準（tech-watch の判断ルール）

提案は次を**すべて**満たすときに Issue 化する:

1. **目的適合** — 上記スコープ内ドメインのいずれかを前進させる。
2. **原則非違反** — 設計原則1〜4のいずれも壊さない。
3. **代替優位** — 既存採用技術より明確な利点（品質/軽量/相互運用性/安全性）がある。
4. **現実的な導入コスト** — spike〜medium 規模で試せる。破壊的なら別途 major 提案として扱う。
5. **非重複** — 既存の Open な `tech-watch` / `feature-sync` Issue と重複しない。

## 見直しサイクル

- **四半期ごと**に `charter-review` ワークフローが「Charter review」Issue を起票し、
  landscape の変化（新仕様・新フレームワークの台頭）と本憲章の改訂案を提示する。
- 初期の目的だけに縛られないよう、スコープ内ドメイン・スコープ外・採用基準を毎回見直す。
- 憲章の変更は PR で行い、CODEOWNERS（`docs/CHARTER.md`）のレビューを必須とする。

_Last reviewed: 2026-07-04 (初版)_
