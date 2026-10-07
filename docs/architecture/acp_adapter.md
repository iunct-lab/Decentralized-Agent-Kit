# ACP の窓口（`dak-cli acp`）: Agent Client Protocol と DAK の API の対応

PBI #314 / Task #315。Zed や JetBrains など Agent Client Protocol（ACP）に対応したエディタから DAK と会話し、
ツールの承認・拒否をエディタの中で選べるようにするための対応を決める。この文書は設計の判断までを扱い、
コードは変えない（実装は #316・#317）。

- §1 置き場所: `dak-cli acp` のサブコマンドにする
- §2 agent との間: ADK の HTTP API と `/approvals` を使い、A2A を使わない
- §3 対応表: ACP のメソッド・通知 → DAK の API・イベント
- §4 使わない機能と理由
- §5 確かめたイベントの例（fake-LLM 構成）
- §6 Python の版と SDK の版

調べたもの（2026-10-07）:

- 仕様: https://agentclientprotocol.com の [Initialization](https://agentclientprotocol.com/protocol/initialization)、
  [Prompt Turn](https://agentclientprotocol.com/protocol/prompt-turn)、[Tool Calls](https://agentclientprotocol.com/protocol/tool-calls)。以下「仕様の Initialization」などと書く
- 公式 Python SDK `agent-client-protocol` 0.12.1（PyPI、2026-08-16、Apache-2.0、Python `>=3.10,<3.15`、依存は `pydantic>=2.7` だけ）。
  import 名は `acp`。スキーマは `schema-v1.19.0` から生成（`acp/meta.py`）、`PROTOCOL_VERSION = 1`。以下 `acp/<file>` はこの版の wheel の中のパス
- DAK 側: `cli/src/client.py`、`agent/dak_agent/server.py`（`/approvals`）、`docs/design/approval-queue.md`、
  google-adk 2.8.0 の `google/adk/cli/api_server.py`（`/run` と `/run_sse`）

## 1. 置き場所: `dak-cli acp`

| 案 | 起動のされ方 | agent とのつながり | 判断 |
|---|---|---|---|
| **`dak-cli acp`（採用）** | エディタが手元の子プロセスとして `dak-cli acp` を起動し、標準入出力で JSON-RPC を話す | `dak-cli` がすでに持つ HTTP のクライアント（`AgentClient`）で agent の HTTP API へ | 採用 |
| 新コンテナ `acp/` | エディタは子プロセスしか起動しないので、コンテナを直接は使えない。`docker run -i` を包むか、コンテナを常駐させて手元に stdio ↔ TCP の中継が要る | 同じ HTTP API | 不採用 |

- ACP はエディタ（クライアント）がエージェントを**子プロセスとして起動し、標準入出力**で話す仕様（仕様の Initialization の前提、SDK の `acp.spawn_agent_process` も子プロセスを起動して stdio でつなぐ）。手元で動くコマンドが合う
- `dak-cli` はすでに agent に HTTP だけでつながり（`cli/src/client.py`）、agent のコードを import しない。サブコマンドにしても独立コンテナ・疎結合の決まり（`AGENTS.md` の「Loosely Coupled」）を崩さない
- ログイン（`~/.dak-cli/config.json` の利用者名と agent の URL）を `dak-cli login` と共有できる。新コンテナでは設定をもう一度渡す口が要る
- 新しいコンテナは、それ自体を配る・起動する手間が増えるだけで、上の中継の分だけ部品が増える

## 2. agent との間: ADK の HTTP API と `/approvals`（A2A は使わない）

- 会話は ADK の `POST /run_sse`（`"streaming": false`）。`dak-cli` の `run_task` が使う `POST /run` と同じ本文で、イベントを 1 つずつ SSE の `data:` 行として返す。`/run` は全部のイベントをターンの終わりにまとめて返すので、エディタに進み具合（文の断片・ツール呼び出し）を順に見せられない
- セッションは ADK の `POST /apps/dak_agent/users/{user}/sessions`（`AgentClient._ensure_session` と同じ）
- 承認の答えは `POST /approvals/{id}/reply`（`agent/dak_agent/server.py`）。`docs/design/approval-queue.md` の 3 のとおり、`/run` に `adk_request_confirmation` の functionResponse を直接送る答え方は、同じ承認への二重の答え（ツールが 2 回動く）を止められないので、CLI も #405 で `/approvals` に移った。ACP の窓口も同じ経路で答える。#317 の手順 1 の「`run_task` の `tool_approval` の分岐と同じ形の functionResponse を `stream_events` で送る」は、この分岐が #405 で無くなっているため、`AgentClient.reply_approval`（`mode` は `once` / `reject`）に置き換える。`reply_approval` は次の確認があると events の配列ではなく `{"status": "needs_approval", …, "response": <events>}` を返すので、その `response` を events として読む（`cli/src/client.py` の `_needs_approval`）
- A2A を使わない理由: DAK の承認は ADK のセッションのイベント（`adk_request_confirmation`）と `/approvals` の上にあり（#100・#101）、A2A の窓口からは答えられない。要望 #117 の「ACP = 人が使う IDE 面、A2A = エージェント間」のとおり、A2A はエージェント間の窓口として分けたままにする

## 3. 対応表

左が ACP（向き: C→A はエディタからエージェントへ、A→C はその逆）、右が DAK。出典の「§5 (x)」はこの文書の §5 で取ったイベント。

| ACP | 向き | DAK の API・イベント | 出典 |
|---|---|---|---|
| `initialize` | C→A | agent には問い合わせない。`protocolVersion: 1`（エディタが 1 以外を求めても、仕様どおり自分の最新の 1 を返す）、`agentCapabilities` は `loadSession: false`、`promptCapabilities` は画像・音声・埋め込みなし、`mcpCapabilities` は http・sse なし、`authMethods: []` | 仕様の Initialization（「supports the requested version … MUST respond with the same version. Otherwise … the latest version it supports」）、`acp/schema.py` の `AgentCapabilities` の既定 |
| `authenticate` | C→A | 使わない（`authMethods: []` なので呼ばれない）。呼ばれたら空の応答。ログインは `dak-cli login` で済ませておく | 仕様の Initialization |
| `session/new`（`cwd`・`mcpServers`） | C→A | `POST /apps/dak_agent/users/{user}/sessions`。返った DAK のセッション ID を ACP の `sessionId` にする。`cwd`・`mcpServers` は使わず、標準エラーに 1 行ログを出す（§4）。ログインしていなければ JSON-RPC のエラーで「`dak-cli login` を先に」と返す | `cli/src/client.py` の `_ensure_session` |
| `session/prompt`（`prompt` の content blocks） | C→A | text の block をつないで `new_message.parts[].text` にし、`POST /run_sse` に送る。`resource_link` の block（仕様で全エージェントが受けるもの）は `uri` を文字として足す（DAK のツールはエディタのファイルを読めないため。§4） | 仕様の Initialization（「All Agents must support … Text and ResourceLink」） |
| イベントの text の part（author がエージェント、`partial` でない） | A→C | `session/update` の `agent_message_chunk`（`content` は text の block）。`content` の無いイベント（ターンの最初の `stateDelta` だけのものなど）は何も送らない | §5 (a) |
| イベントの text の part で `thought: true` | A→C | `agent_thought_chunk` | ADK の `Part.thought` |
| `functionCall`（`adk_request_confirmation` 以外） | A→C | `tool_call`: `toolCallId` = functionCall の `id`、`title` = ツール名、`kind` は `other`、`status: in_progress`、`rawInput` = `args` | §5 (b) |
| `functionResponse`（同じ `id`） | A→C | `tool_call_update`: `toolCallId` = functionResponse の `id`、`status` は `completed`。`response` に `error` があるか、MCP の `isError` が `true` か、`observation`（`denied_by_policy`・`denied_by_user`・`unknown_tool`・`blocked_by_hook` など、実行されなかった・失敗した印）があれば `failed`。ただし `observation` が `hook_rewrote_input` / `hook_rewrote_output`（フックが引数か結果を書き換えた印。`agent/dak_agent/harness.py`）のときは、包んでいる `result` で同じように判定し、文字もその `result` から取る、`rawOutput` = `response`、`content` に結果の先頭（文字。MCP のツールは `response.content[].text`、組み込みのツールは `response.result`）。ただし、同じイベントの `actions.requestedToolConfirmations` にその `id` があるもの（確認待ちの `{"error": "This tool call requires confirmation, …"}`）は `failed` にせず `pending` のままにする | §5 (b)(c)、仕様の Tool Calls（「All fields except toolCallId are optional in updates」） |
| `functionCall` が `adk_request_confirmation` | A→C | `tool_call` は出さない（元の呼び出しの `tool_call` はその前の `functionCall` で出ている）。確認の `id` と元の呼び出しの `id`（`args.originalFunctionCall.id`）を覚えておき、SSE が閉じたら（§3.3）`GET /approvals` で保留を確かめ、`session/request_permission` を送る（`toolCall` は元の呼び出しの `toolCallId` に `status: pending`、選択肢は §3.1） | §5 (c)、`docs/design/approval-queue.md` の 1 |
| `session/request_permission` の答え `selected` | C→A | 選ばれた `optionId` を `POST /approvals/{id}/reply` の `mode` にする（`allow` → `once`、`reject` → `reject`、理由は空）。応答の events（`/run` と同じ形の配列）を同じ変換で `session/update` にする。元のツールの `functionResponse`（許可なら実行結果、拒否なら `{"observation": "denied_by_user", …}`）で `tool_call_update` が `completed`（拒否なら `failed`）になる。応答にまた確認があれば §3.3 を繰り返す | §5 (c)、`agent/dak_agent/server.py` の `reply` |
| `session/request_permission` の答え `cancelled` | C→A | 答えない（§3.2）。ターンは `cancelled` で終える | 仕様の Prompt Turn（「Client … MUST respond to all pending session/request_permission requests with the cancelled outcome」） |
| `session/prompt` の応答 | A→C | SSE（承認の後は reply の応答）が終わり、答えていない確認が残っていなければ `stopReason: end_turn`。取り消されたら `cancelled`。DAK に `max_tokens` などの区別は無いので、ほかの値は返さない | 仕様の Prompt Turn |
| `session/cancel` | C→A | §3.2 | 仕様の Prompt Turn、§5 (d) |
| `session/update` の `plan` | A→C | 送らない。DAK の `planner`（Enforcer Mode の計画）はツール呼び出しとして `tool_call` に出る。`plan` に写すのは別 PBI | — |

### 3.1 承認の選択肢

| `optionId` | `name` | `kind` | DAK の `mode` |
|---|---|---|---|
| `allow` | 許可 | `allow_once` | `once` |
| `reject` | 拒否 | `reject_once` | `reject` |

`allow_always` / `reject_always` は出さない（§4）。

### 3.2 取り消し（`session/cancel`）

仕様の Prompt Turn は、取り消しを受けたエージェントに「言語モデルへの要求とツールの実行をできるだけ早く止め（SHOULD）」、`session/prompt` に `cancelled` で答えること（MUST）を求める。
DAK の側では、取り消しが来る時点で 2 通りある:

1. **`/run_sse` を読んでいる間**: SSE の接続を閉じ、`cancelled` を返す。ADK の `/run_sse` は、接続が切れると `runner.run_async` を `Aclosing` で閉じる（google-adk 2.8.0 `api_server.py` の `run_agent_sse` の `event_generator` が `GeneratorExit` / `CancelledError` を受けて閉じる。`/run` も `http.disconnect` を見て `worker_task.cancel()` する）。§5 (d) では、最初のイベントを読んだところで接続を閉じると、モデルは 1 回も呼ばれず（fake-LLM への要求 0 件）、セッションにもその後のイベントが残らなかった。ただし閉じるのは次の `await` の時点なので、すでに走っている LLM の呼び出しやツールの実行は、その 1 つが終わるまで止まらないことがある（fake-LLM はすぐ答えるので、この場合は確かめていない）
2. **`session/request_permission` の答えを待っている間**: ターンは確認待ちですでに終わっている（§3.3 の 2。SSE は閉じている）。止めるものは無いので、**承認には答えず**に `cancelled` を返す。拒否を送らないのは、拒否の答えが invocation を再開させ、モデルをもう一度呼ぶから（仕様の「言語モデルへの要求を止める」に反する）。答えなかった承認は保留のまま残り、次の `session/prompt`（新しい発言）で捨てられる（`approval-queue.md` の 1 の最後の点）。それまでのあいだは BFF や `dak-cli approve` からも答えられる（#100 の「どのクライアントからでも答えられる」のとおり）

承認の答えを `/approvals/{id}/reply` に送った後（その中で `/run` がターンの続きを最後まで実行する）に取り消しが来たときは、応答を読むのをやめて `cancelled` を返すが、agent 側の続きは止まらない（`/approvals` の reply は agent の中で ADK の `/run` を `httpx.ASGITransport` で呼び、外の接続が切れたかを見ていないので、中の実行は切れないと読める。コードを読んだ判断で、動かしてはいない）。サーバ側で止めるのはこの PBI の範囲外。

### 3.3 確認待ちの流れ

§5 (c) で分かったこと: 確認の要るツールを呼ぶと、ADK は `adk_request_confirmation` のイベントと、元の呼び出しへの `{"error": "This tool call requires confirmation, please approve or reject."}` の functionResponse を出したあと、**同じターンでモデルをもう一度呼び**、その応答でターンを終える（SSE はそこで閉じる）。`docs/design/approval-queue.md` の 1 の「invocation はここで終わり」とは違い、確認を出した時点では SSE は閉じない（`PermissionPlugin` の `before_tool_callback` が出した確認で確かめた）。そこで:

1. SSE を最後まで読み、その間の `session/update` はふつうに送る（確認待ちの後のモデルの文も届く）
2. SSE が閉じたら、このターンで確認を見たときだけ `GET /approvals` を読む。保留の承認（`kind: "approval"`）の最初の 1 件について `session/request_permission` を送る
3. 答えを `POST /approvals/{id}/reply` に送る。同じ時点で保留の他の承認は、reply が `reject`（理由 `approvals.UNANSWERED_REASON`）で一緒に片づける（#406）。その元の呼び出しは `tool_call_update` の `failed` になり、必要ならモデルがもう一度呼ぶ（そのときまた確認が出る）。エディタに複数の承認を一度に並べないのはこのため
4. reply の応答を変換して送り、その中にまた確認があれば 2 に戻る。無ければ `end_turn`
5. reply が断られたとき（`AgentClient.reply_approval` の `ApprovalError`）:
   - `409`（期限切れ。既定 900 秒、`DAK_APPROVAL_TIMEOUT_SECONDS`）: agent は答えの代わりに `timed_out` を流してターンを最後まで進めている（`approval-queue.md` の 4）。応答に events は無いので、元の呼び出しを `tool_call_update` の `failed` にし、「承認の期限が切れ、エージェントは先へ進んだ」旨の文の断片を送る。その続きでモデルが返した文はエディタに届かない（BFF や `GET /apps/…/sessions/…` では見える）。答え直さない
   - `404`（もう保留に無い。別のクライアントが先に答えた、など）: 同じく `failed` と、その旨の文の断片を送る
   - どちらも、その後に 2 に戻って `GET /approvals` をもう一度読む（`409` の続きや、先に答えたクライアントの続きで、新しい確認が出ていることがある）。無ければ `end_turn`

## 4. 使わない機能と理由

親 PBI #314 のスコープ外と揃える。

| 機能 | 使わない理由 |
|---|---|
| `session/load`（`loadSession`） | 過去のセッションの再開と履歴の送り直しはスコープ外。`loadSession: false` を返す |
| `session/set_mode` | DAK のモード（adaptive mode）との対応はスコープ外。`modes` を返さない |
| `fs/read_text_file`・`fs/write_text_file` | DAK のツールは mcp-server のコンテナ（`/projects` にマウントした場所）で動き、エディタの手元のファイルは触らない。エディタの作業場所と合わせる方法は #15（ローカル実行）と #16（権限の境界）の結果を待つ |
| `terminal/*` | 同上 |
| `session/new` の `cwd` | 同上（DAK の作業場所はエディタの `cwd` ではない）。受け取ってログに出すだけ |
| `session/new` の `mcpServers` | 呼び出し元の MCP を使う話は #136 の範囲（`dak:tools` の `mcp_servers`）。この PBI ではつながない。`mcpCapabilities` も http・sse なしで返す |
| `allow_always` / `reject_always` | 親 PBI のスコープ外（1 回ごとの許可・拒否だけ）。#100・#101 が入り、`/approvals` は `mode: "always"` を受けるので、`allow_always` → `always` は後から足せる（別 PBI） |
| `authenticate` | DAK の利用者は `dak-cli login` の設定で決まる。`authMethods: []` |
| 画像・音声・埋め込みの入力（`promptCapabilities`） | スコープ外。text と `resource_link` だけ |
| `elicitation/create` | DAK の質問待ち（`ask_question`、Enforcer Mode）との対応はスコープ外 |
| `plan` の更新 | §3 の最後の行 |

## 5. 確かめたイベントの例

2026-10-07、main 2d1b392 の fake-LLM 構成（`docker-compose.yml` + `docker-compose.test.yml`）で、`POST /run_sse`（`"streaming": false`、本文は `AgentClient.run_task` の `/run` と同じ）の `data:` 行を記録した。
fake-LLM にはモデル `fake-default` の応答を台本で与えた。下はイベントの要点だけを抜いたもの（`invocationId`・`id`・`timestamp`・`usageMetadata`・`nodeInfo` などは省く。`…` は省略）。

### (a) 文だけ

台本: `{"text": "Hello from DAK."}`

```json
{"author": "dak_agent", "actions": {"stateDelta": {"dak_original_request": "hi"}}}
{"author": "dak_agent", "partial": false, "finishReason": "STOP",
 "content": {"role": "model", "parts": [{"text": "Hello from DAK."}]}}
```

1 つ目は `content` の無いイベント（何も送らない）。2 つ目が `agent_message_chunk` になる。SSE はここで閉じ、`end_turn`。

### (b) 組み込みツール `list_skills`

台本: `list_skills` の呼び出し → `{"text": "Listed."}`

```json
{"author": "dak_agent", "actions": {"stateDelta": {"dak_original_request": "what can you do"}}}
{"author": "dak_agent", "partial": false, "longRunningToolIds": [],
 "content": {"role": "model", "parts": [{"functionCall": {"id": "call_eebd365b", "name": "list_skills", "args": {}}}]}}
{"author": "dak_agent",
 "content": {"role": "user", "parts": [{"functionResponse": {"id": "call_eebd365b", "name": "list_skills",
   "response": {"result": "## Curated Skills (Recommended)\n- dependency_maintenance: …"}}}]}}
{"author": "dak_agent", "partial": false, "content": {"role": "model", "parts": [{"text": "Listed."}]}}
```

functionCall と functionResponse は同じ `id`（fake-LLM が付けた `call_…`）で、これを `toolCallId` にする。functionResponse のイベントは `role: "user"` だが `author` はエージェント。

### (c) MCP ツール `write_file`（確認が要る）

既定のツール構成には MCP の `write_file` が入っていない（そのまま呼ばせると `{"observation": "unknown_tool", …}` になった）。モデルが先に `enable_skill` で有効にする。
台本: `enable_skill(skill_name="write_file")` → `write_file(path=…, content="x")` → `{"text": "Wrote it."}`

```json
{"content": {"role": "model", "parts": [{"functionCall": {"id": "call_df3f488c", "name": "enable_skill", "args": {"skill_name": "write_file"}}}]}}
{"content": {"role": "user", "parts": [{"functionResponse": {"id": "call_df3f488c", "name": "enable_skill", "response": {"result": "'write_file' enabled."}}}]},
 "actions": {"stateDelta": {"dak_active_skills": ["write_file"]}}}
{"content": {"role": "model", "parts": [{"functionCall": {"id": "call_11accb99", "name": "write_file", "args": {"path": "acp-capture-6816e3.txt", "content": "x"}}}]}}
{"longRunningToolIds": ["adk-a4f38a26-…"],
 "content": {"role": "model", "parts": [{"functionCall": {"id": "adk-a4f38a26-…", "name": "adk_request_confirmation",
   "args": {"originalFunctionCall": {"id": "call_11accb99", "name": "write_file", "args": {"path": "acp-capture-6816e3.txt", "content": "x"}},
            "toolConfirmation": {"hint": "Please approve or reject the tool call write_file() …", "confirmed": false}}}}]}}
{"content": {"role": "user", "parts": [{"functionResponse": {"id": "call_11accb99", "name": "write_file",
   "response": {"error": "This tool call requires confirmation, please approve or reject."}}}]},
 "actions": {"requestedToolConfirmations": {"call_11accb99": {"hint": "…", "confirmed": false}}}}
{"partial": false, "content": {"role": "model", "parts": [{"text": "Wrote it."}]}}
```

確認の後もモデルが呼ばれ（台本の `Wrote it.` が使われた）、SSE はその後で閉じた（§3.3）。このとき `GET /approvals` は次の 1 件を返した:

```json
[{"id": "adk-a4f38a26-…", "kind": "approval", "tool_name": "write_file",
  "tool_args": {"path": "acp-capture-6816e3.txt", "content": "x"}, "hint": "…", "status": "pending", …}]
```

`POST /approvals/adk-a4f38a26-…/reply` に `{"mode": "once"}` を送ると 200 で、ADK の `/run` と同じ形の配列が返った（元の呼び出しの id で結果が来る）:

```json
[{"content": {"role": "user", "parts": [{"functionResponse": {"id": "call_11accb99", "name": "write_file",
    "response": {"content": [{"type": "text", "text": "Error writing file: [Errno 2] No such file or directory: ''"}], "isError": false}}}]}},
 {"partial": false, "content": {"role": "model", "parts": [{"text": "FAKE_LLM_DEFAULT_RESPONSE"}]}}]
```

ツールは実行された（mcp-server の答えが返った）が、`write_file` はディレクトリを含まないパス（`acp-capture-6816e3.txt`）で失敗した。session sandbox が無効のときは、`_session_path` がパスをそのまま返し、`os.makedirs(os.path.dirname(path))` が空の文字列で落ちる（`mcp-server/main.py` の `write_file`）。ACP の窓口とは別の不具合なので別の Issue にする。#317 の統合テストは、ディレクトリを含むパス（`acp-it/<名前>.txt` など）に書かせる。

同じ流れで `{"mode": "reject", "reason": "cancelled in editor"}` を送ると、元の呼び出しの結果は次のとおりで、mcp-server には何も書かれなかった:

```json
{"functionResponse": {"id": "call_1d330d43", "name": "write_file", "response": {"observation": "denied_by_user", "reason": "cancelled in editor"}}}
```

### (d) ターンの途中で接続を切る

台本: `list_skills` の呼び出し → `{"text": "AFTER_DISCONNECT"}`。`/run_sse` の最初の `data:` 行（(a) の 1 つ目と同じ `stateDelta` だけのイベント）を読んだところで接続を閉じ、5 秒待ってからセッションを読んだ。

- セッションのイベントは user の `go` と、`content` の無いエージェントのイベントの 2 つだけ。`list_skills` の呼び出しも `AFTER_DISCONNECT` も無い
- fake-LLM の `GET /requests/fake-default` は 0 件（モデルは 1 回も呼ばれていない）

接続を閉じると agent 側のターンも止まる（§3.2 の 1）。

## 6. Python の版と SDK の版

- SDK は `agent-client-protocol` の 0.12 系（`>=0.12.1,<0.13`）を使う。2026-09 に 1.0.0rc1・rc2 が出ているが、リリース候補なので使わない。1.0 が出たら依存の更新（Dependabot と `dependency-triage`）で上げる
- SDK は Python 3.10 以上を求めるので、`cli/pyproject.toml` の `requires-python` を `">=3.9"` から `">=3.10"` に上げる（#316）。Python 3.9 は 2025-10 に保守が終わっている
- CI の unit ジョブ（`.github/workflows/ci.yml`）は `astral-sh/setup-uv` で `uv sync` するだけで、Python の版を固定していない。uv は `requires-python` を満たす Python を選ぶので、下限を上げてもジョブの書き換えは要らない。統合テスト（`tests/integration/pyproject.toml`）はすでに `>=3.11`
- ライセンス: SDK は Apache-2.0（DAK と同じ）。SDK の依存は pydantic（MIT）だけで、`cli/uv.lock` には今 pydantic が無いので、pydantic とその依存（pydantic-core など）も新しく入る。`docs/maintenance/license-policy.md` の検査は #316 の `uv lock` の後に通す
