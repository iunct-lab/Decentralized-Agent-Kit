# ローカル実行モード: CLI からエージェントをプロセス起動するか

PBI #15。`dak-cli` から、常駐の agent コンテナ（HTTP）を通さずにエージェントを動かす「ローカル実行モード」を
入れるかを決める。この文書は採否の判断までを扱い、コードは変えない。

- §1 比べた 3 案と比較表（#227）
- §2 二重メンテ回避の方針（コード共有境界）
- §3 #17（CLI の単発呼び出し契約）との関係
- §4 #16（権限境界）との関係
- §5 採否の案
- §6 未検証事項

元の動機（PBI #15 の移行前の記録）: Claude Code は CLI とエージェントが一体なので他のシステムから呼びやすい。
DAK は agent / mcp-server / bff が常駐コンテナに分かれているので、手軽な単発呼び出しがしづらい。

## 1. 比べた 3 案（#227、2026-10-09、main 33acf15）

- **A: HTTP 経由を維持（今）**。`cli/src/client.py` の `AgentClient` が `requests` で agent の公開 API
  （`/apps/dak_agent/users/.../sessions`・`/run`・`/run_sse`・`/approvals`）を叩く。CLI は `dak_agent` を import しない
- **B: CLI プロセスの中で `app` を直接動かす**。`agent/dak_agent/agent.py` の `app`（`App` + `PermissionPlugin` +
  `ContextHarnessPlugin`）を import し、`google.adk.runners.Runner(app=..., session_service=...)` で 1 ターン動かす
  （`agent/tests/test_harness.py` が使う形）
- **C: CLI が agent サーバを子プロセスで代理起動する**。`uvicorn dak_agent.server:app`（`agent/Dockerfile` の起動と同じ）
  を手元で起動し、立ち上がったら A と同じ HTTP で叩く

### 計測（手元、2 vCPU / 3 GB、aarch64。各 1〜3 回、ばらつきは見ていない）

| 項目 | 値 |
|---|---|
| `import dak_agent.agent`（MCP に届かない URL、Langfuse の鍵なし） | 初回 15.5 秒、2・3 回目 8.1 秒、RSS 約 290 MB |
| うち重いもの（`python -X importtime`） | litellm 3.4 秒、mcp.client 1.2 秒、google.adk.models.lite_llm 0.6 秒、google.genai 0.5 秒 |
| `dak-cli --help`（今の CLI） | 初回 2.0 秒、2 回目 0.5 秒 |
| venv の大きさ | agent 425 MB / cli 28 MB |

Lambda（S1）の Init も 8.6 秒から上限 10 秒超えだった（`docs/architecture/serverless.md` §3、#252）。同じ import が B・C の毎回の起動にかかる。

### 比較表

| | A: HTTP 経由を維持 | B: CLI プロセス内で `Runner` | C: agent サーバを代理起動 |
|---|---|---|---|
| 起動コスト | CLI 0.5〜2 秒 + 1 ターン。agent は常駐 | 毎回 8〜15 秒の import + MCP の接続 + 1 ターン | B と同じ import + サーバの起動待ち。常駐させれば A と同じで、常駐のさせ方が 1 つ増えるだけ |
| 状態管理（セッション） | agent の `SESSION_SERVICE_URI`（compose では PostgreSQL）。`run`・`chat`・`approvals`・`approve`・`acp`・BFF が同じセッションを見る | `InMemorySessionService` ではプロセスの終了で消え、確認の保留（`adk_request_confirmation`）に別の呼び出しから答えられない。残すには PostgreSQL に直接つなぐ（compose は postgres をホストに出していない）か、手元の SQLite（常駐の agent・BFF とは別の履歴） | 子プロセスに `SESSION_SERVICE_URI` を渡す。手元の SQLite なら B と同じく別の履歴 |
| 承認の口（`/approvals`） | ある（`agent/dak_agent/server.py`） | 無い。FastAPI のルートなので、CLI 側に一覧と返答を作り直す | ある |
| 設定（モデル・MCP・skills・A2A peer） | agent コンテナの環境変数だけ | CLI の環境に同じ環境変数が要る（`agent.py` が import 時に読む）。skills の既定は `agent/skills` の相対パス | B と同じ |
| 独立コンテナ / CLI の公開 API 境界 | 保つ。CLI は HTTP の公開 API だけに依存 | 崩す。CLI が `dak_agent` の内部（import 時の組み立て、App の名前、環境変数の一覧）と google-adk・litellm に依存し、依存が 28 MB → 425 MB | コードは HTTP で分かれるが、CLI が agent パッケージの配布物と起動の仕方に依存し、依存の大きさは B と同じ |
| 権限（#16） | agent の `PermissionPlugin` が判断 | `app` を使う限り同じプラグインが効く | 同じ |
| #17 の単発呼び出し契約 | 前提どおり | 結果を CLI の中の `Event` から作り直すので、HTTP と同じ結果の形を別のコードで保つ | 前提どおり |

## 2. 二重メンテ回避の方針（コード共有境界）

元の要件は「現コンテナ構成と二重メンテにならない形」。境界は次のとおりに置く:

- CLI（`cli/`）は `dak_agent` を import しない。agent とは HTTP の公開 API（ADK の REST と `/approvals`）だけでつながる。
  これは今の `AgentClient` と `dak-cli acp`（`docs/architecture/acp_adapter.md` §1・§2。エディタからは子プロセスだが、agent とは HTTP）がすでに守っている形
- エージェントの組み立て（`agent/dak_agent/agent.py` の `root_agent` / `app`）は agent プロセスの中の 1 か所だけ。
  CLI 用にもう 1 つの組み立てを作らない
- B を採ると、この 2 点が両方とも崩れる（CLI が `app` を import し、`/approvals` と結果の形を CLI 側にもう 1 つ持つ）。
  C は 1 点目を保つが、CLI の配布物に agent を同梱することになる

## 3. #17（CLI の単発呼び出し契約）との関係

#17 の決定ログ（2026-09-21）は、HTTP/A2A から呼ぶときの呼び出しごとの設定（#137 指示と出力スキーマ・#136 ツール選択・
#138 モデル・#134 上限・#140 検査）の**結果の形に、CLI の単発呼び出し契約を揃える**方針。
`docs/architecture/cli_call_contract.md` §3-4 はこれを受け、構造化された失敗（`{"error": <種別>, ...}`）を HTTP・A2A・CLI で同じ名前・同じキーで判定できるようにしている。

- `dak-cli run`（`cli/src/main.py:103`）の stdin・`--format json`・exit code の契約（同文書 §2）は、`AgentClient` が HTTP で得た ADK のイベントを前提にしている
- B を採ると、CLI は HTTP を通らずに `Runner` のイベントから結果を作るので、HTTP/A2A と同じ結果の形をもう 1 つのコードで保つことになる（契約の二重化）。#17 の前提が崩れる
- A・C は #17 の前提のまま。元の動機の「手軽な単発呼び出し」は、#17（stdin・機械可読の出力・exit code）と #314（`dak-cli acp`）が HTTP 経由のまま扱っている

## 4. #16（権限境界）との関係

`docs/design/permission-boundary.md` の決定 1（#166）: 許可・確認・拒否の判断は agent 側の `before_tool_callback`
（App のプラグイン `PermissionPlugin`、#101 で実装）に一元化し、ローカル・リモートの MCP を問わず agent を通る呼び出しに当てる。

- 判断はいつも `app` を動かすプロセスの中にあるので、実行方式（HTTP / プロセス起動）を変えても、この結論は変わらない。B でも `app` を使う限り同じプラグインが効く
- 変わるのは確認（ask）への答え方。A・C は `/approvals`（`docs/design/approval-queue.md`）で、どのクライアントからも答えられる。B は確認の保留をプロセスの外に出す口が無いので、同じプロセスの中で答えるか、口を作り直すことになる（§1 の比較表）

## 5. 採否の案

**案: ローカル実行モード（B・C）は入れない。A（HTTP 経由）を維持する。**（利用者の判断待ち。PBI #15 の `## 判断待ち`）

根拠:

1. 独立コンテナの規約（CLAUDE.md「独立コンテナ/疎結合を壊さない」）。B は CLI を agent の内部実装に依存させる。C もコードは分かれるが配布物で結びつく（§1、§2）
2. 状態と承認。B は確認の保留と `/approvals` を失うか作り直しになり、セッションを残すには DB に直接つなぐ（§1、§4）
3. 起動コスト。B・C は毎回 8 秒超の import がかかり、今の CLI（0.5〜2 秒 + 1 ターン）より遅い。常駐させれば A と同じになる（§1）
4. #17・#314 が HTTP 経由のまま単発呼び出しを進めており、B はその契約を二重化する（§3）

見直す条件: agent の import が数百ミリ秒に収まる、または docker compose を使えない利用者の要件が出たとき。
そのときも C（サーバを代理起動して HTTP で叩く）から検討し、B（CLI が `app` を import する）は §2 の境界を崩すので選ばない。

## 6. 未検証事項

- B の起動時間を、MCP に実際につながる構成で測っていない（今回は届かない URL。MCP のツール一覧の取得の時間は入っていない）
- Langfuse の鍵があるときの `auth_check` の通信（`agent/dak_agent/patches.py`）の分は入っていない
- C のサーバの起動待ち（uvicorn が待ち受けるまで）の時間は測っていない
- 数値は aarch64 の 2 vCPU の 1 台だけ。x86_64 や開発者の手元では測っていない
