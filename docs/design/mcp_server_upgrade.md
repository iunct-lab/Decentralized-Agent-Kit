# mcp-server: mcp 2.x の MCPServer に移すか

PBI #39。mcp-server の `mcp` 依存を 1.30 から 2.x に上げ、`FastMCP` を `MCPServer` に置き換えるかを記録する。
エージェント側（`agent/pyproject.toml`）は既に `mcp>=2.3,<3`。

## 結論

**採用する**（2026-10-15、mcp 2.3.0）。`mcp-server/pyproject.toml` は `mcp>=2.3,<3`、`main.py` は 2.x に一本化した。
1.x と 2.x の両対応の層は持たない。

## 根拠

- #211（PR #809）: `cd mcp-server && uv run pytest -q` が 114 件 green。DNS rebinding 保護の許可・拒否
  （`tests/test_transport_security.py`）も 2.x のまま通る
- ツールの名前・引数・戻り値は変えていない（11 個。エージェントと統合テストが今の名前で呼ぶ）
- 統合構成（fake-LLM、`tests/integration/test_mcp_server.py` を含む）: PR #809 の CI の `integration` が pass（4m43s）
- 移すと #322・#326・#629 が進められる（下の表）

## 1.30 から変わった点

| 面 | 1.30 | 2.x | main.py |
|---|---|---|---|
| import | `mcp.server.fastmcp` の `Context, FastMCP` | `mcp.server.mcpserver` の `Context, MCPServer` | `:10` |
| 生成 | `FastMCP("dak-agent-mcp", json_response=True, transport_security=…)` | `MCPServer("dak-agent-mcp")` | `:42` |
| `json_response` / `transport_security` | 生成時の引数 | `streamable_http_app()` の引数 | `:608` |
| セッション管理 | `mcp.session_manager.run()` | 同じ | `:599` |
| ツール登録 | `@mcp.tool()` | 同じ | 11 箇所 |

## 依存の制約

- `mcp-server/pyproject.toml`: `mcp>=2.3,<3`。major の更新は破壊的変更を含みうる（semver）ので上限を残す
- `.github/dependabot.yml`（mcp-server の `mcp`）: major の無視は**変えない**。2.x に移った後は 3.x の提案を止める
  ことになり、次の major も移行 PR で扱う。minor / patch はグループ `mcp-minor-patch` で届く
- `.github/dependabot.yml:24-25` のコメント（「mcp 2.x は main.py の移行が必要」）は移行後は古い。`.github` を触る PR は
  マージ判定で利用者に聞くことになり、挙動は変わらないので、意図して直していない。無視を外さないこと

## この移行を前提にする PBI

| PBI | Task |
|---|---|
| #322 | #324、#325 |
| #326 | #327 |
| #629 | #212 |

## `Session terminated`（旧 #39 の調査）

旧 #39 は enable_skill が足す絞り込みの McpToolset で `Session terminated` になる回帰を扱っていた。
原因のセッションは #629 がなくす（セッションなしの 2026-07-28 版でつなぐ）。その確認は #629 の受け入れ条件 3
（Task #212）で行い、この文書では調べない（#808）。
