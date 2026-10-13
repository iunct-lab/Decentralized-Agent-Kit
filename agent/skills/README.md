# DAK ランタイムスキル

このディレクトリの各スキルは、DAK のエージェントが実行時に `enable_skill` で有効にする機能です
（`agent/dak_agent/skill_registry.py` が読み込む）。コーディングエージェント向けのスキルとは別物です。
追加の置き場所は環境変数 `AGENT_SKILLS_DIRS`（`:` 区切り）で指定できます。

## 作り方

参考実装: `solana_wallet/`（`SKILL.md` のフロントマター + `tools.py` の関数群）、`dependency_maintenance/`（自己保守スキル）。

1. `agent/skills/<name>/SKILL.md` を作る（`<name>` は snake_case）。
   - フロントマターに `name`、`description`、`tools:`（公開する関数名の一覧）を書く。
   - 本文にはエージェント向けの使い方と制約を書く。暗黙の副作用を持たせない。
2. `agent/skills/<name>/tools.py` に、文字列を返すツール関数を実装する。docstring は LLM が読む前提で、引数と戻り値を明記する。
   `tools.py` に無い名前は MCP サーバのツールとして読み込まれる。
   既定の mcp-server ではなく `agent_config.yaml` の `mcp_servers` の 1 つから取るなら、フロントマターに `mcp_server: <name>` を書く。
   その名前が `mcp_servers` に無い（`requires_env` の環境変数が未設定で外れたときも）と、スキルは `list_skills` に出ず、`enable_skill` は理由付きのエラーを返す。既定の mcp-server には取りに行かない。
3. `agent/tests/skills/` に読み込みと動作の単体テストを足す（既存の `test_*` を参考に）。
4. `cd agent && uv run pytest -q`。必要なら統合テストでも確認する。

「System ENABLES, Agent DECIDES」を守る。危険な操作は Observation を返し、判断をエージェントに委ねる。
