# Web 検索（Tavily）

エージェントの `web_search` スキル（`agent/skills/web_search/SKILL.md`）は、Tavily のリモート MCP サーバのツール `tavily_search`・`tavily_extract` を使って Web を調べる。
**自分の Tavily の API キーを `TAVILY_API_KEY` で渡したときだけ**有効になる。渡さなければ `list_skills` に出ず、`enable_skill("web_search")` は理由付きのエラーを返し、Tavily へは一度も接続しない。

## 有効にする

1. https://app.tavily.com でキーを取る（無料枠はクレジットカード不要）
2. `.env` に書く（`.env` は Git の管理外。キーをリポジトリのファイルに書かない）:
   ```bash
   TAVILY_API_KEY=<自分のキー>
   ```
   Switchboard のホストでは `.env` に書かない。AGENTS.md の「Switchboard-hosted credentials」に従い、SSM Parameter Store の `SecureString` から起動時に環境変数として渡す
3. `docker compose up -d --build`（agent を作り直すと環境変数が入る）
4. エージェントに頼む。例: 「Web 検索のスキルを有効にして、〜を調べて」。エージェントが `enable_skill("web_search")` → `tavily_search` と進む

検索のたびにクレジットを使う（下の「利用条件」）。試す回数は自分で決める。

### 仕組み

`agent/agent_config.yaml` の `mcp_servers` の `tavily`:

| 項目 | 値 | 意味 |
|---|---|---|
| `url` | `https://mcp.tavily.com/mcp/` | Tavily のリモート MCP（streamable HTTP） |
| `requires_env` | `["TAVILY_API_KEY"]` | 未設定か空なら、このサーバとそれを使うスキルは無いものとして扱う |
| `bearer_token_env` | `TAVILY_API_KEY` | キーを `Authorization: Bearer <キー>` ヘッダで送る |
| `url_env` | `TAVILY_MCP_URL` | 設定すると `url` を置き換える（テストの偽のサーバ向け） |

キーは URL の `?tavilyApiKey=` では渡さない（URL はログに出やすい）。キーの値は設定ファイル・ログに出ない（`agent/tests/test_mcp_server_env.py`）。
このサーバには `X-DAK-Session-Key`（利用者 ID とセッション ID）を送らない（`send_session_key` を書いていない。`docs/design/session-sandbox.md`）。
`docker-compose.yml` は `TAVILY_API_KEY`・`TAVILY_MCP_URL` を値なしで素通しする（未設定はコンテナでも未設定のまま）。

保守の自動化（`maintenance/` の `tech-watch`・`charter-review`）も `TAVILY_API_KEY` を使うが、REST API を直接呼ぶ別の用途で、キーは GitHub の secrets に別に置く。エージェントの `.env` のキーとは別に管理する。

## 接続方式とその理由

Tavily の公式のリモート MCP に HTTP でつなぐ。tavily-mcp を git submodule にしたり、DAK のイメージに同梱したりはしない（PBI #330）。

- DAK に Tavily のコードを取り込まないので、再配布のライセンス上の義務が生じない
- 公式の `Dockerfile` は `node build/index.js`（stdio）で起動する。DAK のエージェントは HTTP（streamable HTTP / SSE）の MCP にしかつながらないので、自前で動かすには stdio を HTTP に変える中継と Node のコンテナが別に要る
- リモートが使えない事情が出たら、自前で動かすことを別の PBI で考える

## tavily-mcp のライセンス

https://github.com/tavily-ai/tavily-mcp （2026-10-13 確認、`package.json` の版 0.2.23）

- MIT License（`LICENCE`）。Copyright (c) 2024 Alpha AI Technologies Inc.
- DAK はコードを取り込まずリモートに接続するだけ
- ツールの名前は `tavily_search`・`tavily_extract`・`tavily_crawl`・`tavily_map` など（`src/index.ts`。2026-01-20 にハイフンからアンダースコアに改名。README の一部は古い名前のまま）。`web_search` スキルは `tavily_search`・`tavily_extract` だけを使う（`tavily_crawl`・`tavily_map` は 1 回の消費が大きくなりうる）

## Tavily の利用条件

- クレジット（https://docs.tavily.com/documentation/api-credits 、2026-10-13 確認）:
  - 無料枠: 毎月 1,000 クレジット。クレジットカード不要
  - 検索: 基本 1 クレジット、詳細（advanced）2 クレジット / 回
  - 抽出: 基本で成功した URL 5 件ごとに 1 クレジット（詳細は 2）。失敗した抽出は数えない
  - 従量: 1 クレジット 0.008 ドル
- 利用規約（https://tavily.com/terms 、Last updated: May 4, 2026。2026-10-13 確認）: API とキーは契約者自身の利用に限り、第三者と共有してはならない。キーは安全に保管する
  - DAK は**各利用者が自分のキーを渡す**前提。キーを他人に配ったり、共有のキーを同梱したりしない

## 検索結果の扱い

検索結果と抽出したページは外部の信頼できない内容。スキルの説明（`SKILL.md`）で、その中の指示に従わないようにモデルに伝えている。
それ以上の対策（外部フック、保護パス）はこの機能の範囲外。
