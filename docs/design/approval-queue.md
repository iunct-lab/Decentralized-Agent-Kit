# 承認待ち・質問待ちの保留: 一覧・reply 契約・タイムアウト

PBI #100 / Task #172。この文書は、承認待ち（ツールの確認）と質問待ち（`ask_question`）を**どのクライアントからでも一覧して答えられる**ようにするための方式を決める。
後続の Task（#173 / #174 / #175）はここで決めた名前と形で作る。

調べた版: google-adk 2.8.0、mcp（agent の依存）1.28.1。パスは `agent/.venv/lib/python3.12/site-packages/` からの相対（以下 `<adk>` = `google/adk`）。
Task 本文の `python3.13` は誤りで、agent の venv は 3.12。

## 1. 事実: ADK の確認フローは、もうセッションに紐づく非同期の保留である

- ツールが確認を求めると、`tool_context.request_confirmation(hint=...)` を呼び、ツールの結果として `{"error": "This tool call requires confirmation, please approve or reject."}` を返す（`FunctionTool`: `<adk>/tools/function_tool.py:330-347`、`McpTool`: `<adk>/tools/mcp_tool/mcp_tool.py:399-413`）
- その結果から、ADK は `adk_request_confirmation` という名前の functionCall を持つイベント（author はエージェント、`longRunningToolIds` にその id）を作る（`<adk>/flows/llm_flows/functions.py:402-444`）。args は次の形:

  ```json
  {"originalFunctionCall": {"id": "<元の呼び出しの id>", "name": "<ツール名>", "args": {...}},
   "toolConfirmation": {"hint": "...", "confirmed": false}}
  ```

  long running の呼び出しなので invocation はここで終わり、`POST /run` の応答も返る（HTTP 接続は残らない）
- 答えは、同じ `app_name` / `user_id` / `session_id` への次の `POST /run` の `new_message` に載せた functionResponse:

  ```json
  {"parts": [{"functionResponse": {"id": "<adk_request_confirmation の id>", "name": "adk_request_confirmation",
                                   "response": {"confirmed": true, "payload": {...}}}}]}
  ```

  `_RequestConfirmationLlmRequestProcessor`（`<adk>/flows/llm_flows/request_confirmation.py`）が**最後の user イベント**の functionResponse を読み、`originalFunctionCall` を引いて元のツールを `tool_confirmation` 付きでもう一度実行する（Step 1〜4）。すでに応答済みの元の呼び出しは飛ばす（Step 2）
- したがって「どのクライアントが答えても同じ結果になる」は ADK の標準 REST API だけで成立している。保留はセッションのイベント列そのもので、別のストア（pending dict、`asyncio.Future`）は要らない。今この形で答えているクライアントは CLI だけ（`cli/src/client.py:83-102`、`response` は `{"confirmed": bool}` のみ）
- 答えずに新しい文を送ると、最後の user イベントが functionResponse でなくなるので、その確認は二度と処理されない（Step 1 で `return`）。保留は「最後の user イベントより後にある `adk_request_confirmation`」だけ（答えはそれ自体が user のイベントなので、それより後の確認は全部未回答）
- `ToolConfirmation`（`<adk>/tools/tool_confirmation.py`）のフィールドは `hint` / `confirmed` / `payload` の 3 つだけ（`extra="forbid"`）。`once` / `always` / `reject` と理由は DAK が `payload` に載せる独自の拡張になる
- 拒否（`confirmed: false`）されたツールは、理由を見ずに固定の `{"error": "This tool call is rejected."}` を返す（`function_tool.py:350-351`、`mcp_tool.py:414-415`。vendor のコードなので変えない）
- ただし、もう一度実行されるときの `ToolContext` は答えの `ToolConfirmation` を持つ（`functions.py:1334-1344` の `_create_tool_context`）。**エージェントの `after_tool_callback` は `tool_context.tool_confirmation.payload` から理由を直接読める**。答えるときに理由を session state に退避しておく必要は無い（#174 の手順 3・4 を変える。下の「Task への影響」）
- after_tool_callback の順番: App のプラグインの `after_tool_callback` が先で、値を返すとエージェントの callback は呼ばれない（`functions.py:652-683`）。今のプラグイン `ContextHarnessPlugin.after_tool_callback`（`agent/dak_agent/harness.py:719`）は、短い dict（固定の拒否文言）には `None` を返すので、エージェントの callback まで届く

### 今どのツールが確認を求めるか

`docs/design/permission-boundary.md` の「現状」のとおり、**今の main では MCP のツール（`run_command` など）は確認を求めない**（`require_confirmation=True` のツールセットはモデルに渡らない）。確認を MCP のツールに付けるのは #101（agent 側の `before_tool_callback`）。
今、確認を求めるのは `planner` だけで、`DAK_PLANNER_REQUIRE_CONFIRMATION=true` のとき（`agent/dak_agent/builtin_tools.py:192-218`）。
#101 が入ると確認は `before_tool_callback` の `tool_context.request_confirmation(...)` から出るが、イベント列の形（`adk_request_confirmation` の functionCall）は同じなので、この文書の一覧・reply はそのまま使える。拒否をどう返すか（固定文言か、callback が理由つきで返すか）は #101 が決める。

追記（2026-09-30）: #101 / #178 が main に入った。`PermissionPlugin`（`agent/dak_agent/permission.py`。App のプラグインの `before_tool_callback`）が、規則が `ask` になるツール（既定の MCP サーバの書き込み・コマンドなど）で `request_confirmation` を呼ぶ。拒否は ADK と同じ固定文言 `{"error": "This tool call is rejected."}` を返すので、理由は 3 の `_restore_reject_reason` でモデルに届く（プラグインが結果を返してもエージェントの `after_tool_callback` は呼ばれる。`functions.py` の Step 4）。`always` は `PermissionPlugin` が `payload.mode` を読んで継続許可を保存する。この文書の一覧・reply はそのまま使える。統合テスト（#175）は MCP サーバに依らない `planner` で保留を作る。

### 質問待ち（`ask_question`）

- `ask_question`（`builtin_tools.py:40-49`）は enforcer mode のときだけ出る普通のツール（確認なし）。`end_invocation = True` にして invocation を終え、結果（質問の文面）を functionResponse として残す
- 答えは次の `POST /run` の自由文（`{"parts": [{"text": "..."}]}`）。これも、どのクライアントが送っても同じセッションが再開する
- `ask_question` を確認フローに作り替えない（`agent/tests/test_builtin_tools.py` の `test_ask_question_ends_invocation` が固定する挙動を残す）。一覧に「質問」として出し、答えを自由文に組み立てるだけを足す

## 2. 決定: 一覧の形

入口は 1 つ: `approvals.list_pending(events) -> list[dict]`（#174）。`events` は ADK のセッションのイベントを
`event.model_dump(mode="json", by_alias=True, exclude_none=True)` にした dict（REST の `GET /apps/{app}/users/{user}/sessions/{session}` が返す `events` と同じ形）。
イベント列を毎回読み、結果を別に保存しない。

| キー | 承認（`kind: "approval"`） | 質問（`kind: "question"`） |
|---|---|---|
| `id` | `adk_request_confirmation` の functionCall の id | `ask_question` の functionCall の id |
| `kind` | `"approval"` | `"question"` |
| `tool_name` | `originalFunctionCall.name` | `"ask_question"` |
| `tool_args` | `originalFunctionCall.args` | — |
| `hint` | `toolConfirmation.hint` | — |
| `questions` / `context` | — | `ask_question` の args |
| `requested_at` | そのイベントの `timestamp`（UNIX 秒） | そのイベントの `timestamp` |
| `status` | `"pending"` か `"timed_out"`（下の 4） | 同じ |

保留に数える条件:

- 承認: 最後の `author == "user"` のイベントより後にある `adk_request_confirmation` の functionCall。答えは user のイベントなので、答え済みの確認はその答えより前にあり、ここに入らない（同じ id の functionResponse を探す必要は無い）
- 質問: 最後の `author == "user"` のイベントより後に `ask_question` の functionCall があるもの（その後に user のイベントがあれば答え済み）

## 3. 決定: reply 契約

reply は `id` と次の本文で受ける（HTTP は #175 の `POST /approvals/{id}/reply`）。どちらも `new_message` に組み立てて `POST /run` と同じ `Runner.run_async` に渡すだけで、保留の状態は持たない。

**reply はまず `list_pending` にその `id` があるかを確かめ、無ければ `404` を返して何も流さない**（答え済み・新しい発言で捨てられた・存在しない）。ADK は二重の答えを止めない: 答え済みかを見るのは「最後の user イベントより後」だけ（`request_confirmation.py` の Step 2）なので、BFF が `once` で答えてツールが動いた後に CLI が同じ `id` へ答えると、その答えが最後の user イベントになり、ツールがもう一度動く。受け入れ条件 1 の「両方のクライアントから答えられる」はこの二重の答えを含むので、`404` で止める。質問も同じ（答え済みの質問への答えは、新しい発言として流れてしまう）。
この確かめは `/approvals` の reply だけに効く。`/run` に直接 functionResponse を送る答え方（今の CLI、`cli/src/client.py:83-102`）は ADK の標準の経路なので、ここでは止められない。そのため CLI の答えも `/approvals/{id}/reply` に移す（#405）。
確かめと実行のあいだに同じ `id` への reply がもう 1 本来ると、両方が確かめを通る。#175 はセッションごとの `asyncio.Lock` を確かめから `Runner.run_async` の終わりまで持って、同じセッションへの reply を 1 本ずつにする（agent は 1 プロセスの uvicorn で動く。複数 worker にするなら、この錠はプロセスをまたがない）。

### 承認: `{"mode": "once" | "always" | "reject", "reason": ""}`

`approvals.build_reply_function_response(fc_id, mode, reason)`（#173）が作る `new_message`:

| DAK の `mode` | `response.confirmed` | `response.payload` | モデルに届く Observation |
|---|---|---|---|
| `once` | `true` | `{"mode": "once", "reason": ""}` | ツールの実行結果 |
| `always` | `true` | `{"mode": "always", "reason": ""}` | ツールの実行結果（永続化は #101 / #178 が `payload.mode` を読んで行う。#100 では `once` と同じ動き。区別はセッションのイベントに残る functionResponse の `payload.mode` と、再実行時の `tool_context.tool_confirmation.payload` で見える） |
| `reject` | `false` | `{"mode": "reject", "reason": "<理由>"}` | `{"observation": "denied_by_user", "reason": "<理由>"}` |
| `timed_out`（4 で DAK が送る） | `false` | `{"mode": "timed_out", "reason": ""}` | `{"observation": "timed_out"}` |

- `confirmed` は必ず入れる。`/run` に payload なしの `{"confirmed": bool}` を直接送るクライアントも ADK の経路として動き続ける（payload が無い拒否は、理由なしの固定文言のまま届く）。CLI はこの直送をやめて `/approvals` に移る（#405）
- 固定文言 `{"error": "This tool call is rejected."}` を Observation に書き換えるのは `AdaptiveAgent` の `after_tool_callback`（#174 の `_restore_reject_reason`）。`tool_context.tool_confirmation` が `confirmed: false` で `payload.mode` が `reject` / `timed_out` のときだけ動き、それ以外は `None`

### 質問: `{"answer": "<自由文>"}`

`approvals.build_question_reply(answer)`（#174）が `{"parts": [{"text": answer}]}` を作る。ADK から見れば普通の次の発言で、どのクライアントの自由文とも同じ。

## 4. 決定: タイムアウトは遅延評価

- 期限は `DAK_APPROVAL_TIMEOUT_SECONDS`（既定 900 秒）。`approvals.is_expired(requested_at)` = `time.time() - requested_at > PENDING_TIMEOUT_SECONDS`（#173）
- 常駐のスケジューラやタイマーは持たない。**次にその保留が触れられたとき**に評価する
  - 一覧: 期限切れの保留は `status: "timed_out"` を付けて返す（消さない。消費するのは reply）。Task #172 の手順 4 は「一覧取得か reply で消費する」だったが、一覧では消費しない: 消費は `timed_out` の答えを流して invocation を再開させること、つまりエージェントと LLM を動かすことで、`GET` と 2 秒ごとに一覧を読む SSE（#175）に副作用とお金のかかる処理を持たせることになる
  - reply: 期限切れの承認に reply が来たら、答えの代わりに `mode: "timed_out"` の functionResponse を流して invocation を再開させ、HTTP は `409` と `{"observation": "timed_out"}` を返す。モデルには `denied_by_user` ではなく `timed_out` が届き、invocation は落ちない。**`409` でも副作用はある**（答えは受け付けなかったが、エージェントは `timed_out` を受けて先へ進んだ）。クライアントは `409` を「何も起きなかった」と読んで答え直さない（答え直しても、その `id` はもう保留に無いので `404`）
  - 期限切れの質問への reply は、期限を理由に拒まない（答えは自由文で、ADK から見て普通の発言。拒むと利用者は同じ文を送り直すだけ）。一覧の `status` だけ `timed_out` にする
- 誰も触らない保留は、次の user の発言で自然に捨てられる（1 の最後の点）。捨てられた保留は一覧に出ない。このときモデルに `timed_out` は届かず、モデルが見るのは元の「requires confirmation」の結果と新しい発言。利用者が答えずに次の話へ進んだのであって、エージェントが待ち続けているわけではない（invocation は確認を出した時点で終わっている）ので、`timed_out` を差し込まない

## 5. MRTR（MCP 2026-07-28、SEP-2322）との対応

一次資料: [SEP-2322 Multi Round-Trip Requests](https://modelcontextprotocol.io/seps/2322)（Status: Final）、[MCP 2026-07-28 のブログ](https://blog.modelcontextprotocol.io/posts/2026-07-28/)。2026-09-29 に読んだ。

MRTR の形: サーバはクライアントの要求（`tools/call` など）に `InputRequiredResult` を返せる。`resultType: "input_required"`、`inputRequests`（キーはサーバが決める。値は `{"method": "elicitation/create" | "sampling/createMessage" …, "params": {...}}`）、`requestState`（クライアントが中を見ずにそのまま返す不透明な文字列）。
クライアントは元の要求を `inputResponses`（同じキーで、`elicitation/create` なら `ElicitResult` = `{"action": "accept" | "decline" | "cancel", "content": {...}}`）と `requestState` を付けてもう一度送る。`resultType` が無ければ `"complete"`。

| DAK | MRTR | 対応 |
|---|---|---|
| 保留の一覧の 1 件 | `InputRequiredResult`（`resultType: "input_required"`） | 形の対応だけ。DAK は一覧を HTTP で見せ、MRTR は要求への応答として返す |
| `id`（functionCall の id） | `inputRequests` のキー | 対応 |
| `kind: "approval"` + `hint` | `inputRequests[k] = {"method": "elicitation/create", "params": {"mode": "form", "message": <hint>}}` | 対応（MRTR に確認専用の型は無く、elicitation で表す） |
| `kind: "question"` + `questions` | `elicitation/create` の `message` / `requestedSchema` | 対応 |
| `mode: "once"` / `"always"` | `ElicitResult.action: "accept"` | `always` の区別は MRTR に無い（DAK の拡張として `content` に載せる想定） |
| `mode: "reject"` + `reason` | `ElicitResult.action: "decline"` | `reason` の置き場は MRTR に無い（同上） |
| `mode: "timed_out"` | `ElicitResult.action: "cancel"` | 対応 |
| `{"answer": ...}` | `ElicitResult{"action": "accept", "content": {...}}` | 対応 |
| `session_id` + `id`（状態は agent のセッション） | `requestState`（状態はクライアントが運ぶ） | **未対応**。DAK は状態を agent のセッションに持つので、不透明な状態をクライアントに持たせない |
| reply で元の invocation を再開 | 元の要求を `inputResponses` 付きで再送 | 形が違う。DAK は元の要求を再送させない |
| — | `sampling/createMessage` の `inputRequests` | **未対応**（範囲外） |
| mcp-server が `input_required` を返したときの透過 | — | **未対応**。agent の mcp（1.28.1）は `InputRequiredResult` を持たない（`mcp/types.py` の `input_required` は Tasks の `TaskStatus` だけ）。ADK の `McpTool` も扱わない。mcp-server 側の対応は #112 で追う |

## Task への影響（PBI #100 の決定ログにも書く）

- #173: 一覧の各件のキー名を `fc_id` ではなく `id` にし、`kind` もここで付ける（質問と同じ一覧に並べるため）。それ以外は Task 本文どおり
- #174: 理由を state に退避しない。`stash_reject_reason` は作らず、`_restore_reject_reason` は `tool_context.tool_confirmation.payload` を読む（1 の事実）。`build_question_reply(answer)` を足す（3 の質問の答え。受け入れ条件 3 の「質問も保留として回答でき」）
- #175: reply は `list_pending` に無い `id` を `404` にし、確かめから実行の終わりまでセッションごとの錠を持つ（3）。一覧（`GET` / SSE）は期限切れを `status: "timed_out"` と示すだけで消費しない。消費は reply だけ（4。Task #172 の手順 4 からの変更）。期限切れの承認への `409` は副作用ありと API の説明に書く（4）。統合テストの保留は `run_command` では作れない（MCP のツールは今は確認を求めない）。`DAK_PLANNER_REQUIRE_CONFIRMATION=true` を test override で与え、fake-LLM に `planner` の functionCall を台本して作る。#101 が入れば同じテストの台本を MCP のツールに替えられる。reply の本文に質問の `answer` を足し、`reject` のときの state 書き込みはしない。期限切れの質問は `409` にしない（4）

- #405（新規）: CLI の承認の答えを `/run` 直送から `/approvals/{id}/reply` に移し、`dak-cli approvals` / `dak-cli approve` で別のクライアントが始めたセッションの保留に答えられるようにする（3）

## 未検証事項

- 1 回のモデルの応答が複数の確認を出したとき、reply が 1 件だけに答えると、残りの確認は捨てられる（答えが最後の user イベントになり、残りはその前に並ぶので一覧から消え、reply は `404`。ADK は最後の user イベントの答えしか読まないので、そのツールは動かず、モデルにも何も届かない）。今は `PermissionPlugin` が MCP のツールにも確認を求め（上の追記）、1 回の応答で確認の要るツールを複数呼ぶことは止めていない（`AdaptiveAgent` にも enforcer にもその制限は無い）ので、今でも起きうる。#100 ではこの制限を残し（どちらのクライアントから答えても同じ結果になる、という受け入れ条件 1 は変わらない）、兄弟の確認をどう扱うか（まとめて答える・残りを一覧に残す）は要件の解釈なので、Task #406（#101 の子）で利用者に確かめて扱う

- この文書の ADK の流れはコードを読んで確かめたもので、動かしてはいない（#175 の統合テストで確かめる）
- 別のエージェント（sub_agent）が出した確認は、そのエージェントの processor が処理する（`request_confirmation.py` の「authored by another agent」）。DAK の sub_agent は A2A の `RemoteA2aAgent` だけ（`agent/dak_agent/agent.py:91-92`）で、ツールの確認はその先のエージェントの中で起きる。こちらのセッションに `adk_request_confirmation` を出すのは `AdaptiveAgent` だけなので、一覧は author を見ない（動かしてはいない）
