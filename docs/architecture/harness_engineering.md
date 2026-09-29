# ハーネスエンジニアリング（コンテキスト管理を中心に）

DAK を「長いタスクでも壊れないエージェントアプリ」にするための設計メモ。
Claude Code や LangChain Deep Agents との差分、ADK 標準機能との対応、実装済みの
コンテキストハーネス、残りのバックログをまとめる。

## 1. 発端: 1 リクエストでコンテキスト超過

「既存のバックログと実装を確認し、品質改善・ハーネスエンジニアリングを整理して」
という依頼を llama.cpp（Qwen 27B, `n_ctx=32768`）構成の DAK に送ると、1 回の
invocation の中で約 20 回のモデル呼び出し（ツールでファイルを読み進める）が
続いたあと、次のエラーで停止した。

```
request (41039 tokens) exceeds the available context size (32768 tokens)
```

### 根本原因

| # | 原因 | 詳細 |
|---|------|------|
| 1 | **既存の保護が死んでいた** | `AdaptiveAgent` は `session.contents` からトークン数を見積もっていたが、ADK の `Session` にあるのは `events` だけ。見積もりは常に 0 になり、「50% でモード切替＋履歴クリア」は一度も発火していなかった（テストは MagicMock で `contents` を差し込んでいたため通っていた）。 |
| 2 | **ツール出力に上限がない** | MCP の `read_file`/`run_command`/`list_files` はファイル全体や出力全体をそのまま返す。さらに ADK の MCP アダプタは FastMCP の `structuredContent` もそのまま渡すため、**同じテキストが 2 回**モデルに入る。 |
| 3 | **invocation 内で圧縮する仕組みがない** | ツールループは 1 invocation の中で履歴が伸び続ける。仮に 1 が動いていても、履歴を `clear()` するやり方は DB セッションに永続化されず、ツール呼び出しと応答のペアも壊しかねない。 |

## 2. ADK に Deep Agents 相当はあるか

**`create_deep_agent()` のような「全部入りハーネス」は ADK 2.8 には無い**。
ただし部品は揃っており、Deep Agents / Claude Code の各機能に対応するものがある。

| ハーネス機能 | Deep Agents / Claude Code | ADK 2.8 の部品 | DAK の状態 |
|---|---|---|---|
| コンテキスト圧縮 | 窓の 85% で要約、`compact_conversation` ツール / auto-compact | `App(events_compaction_config=EventsCompactionConfig(token_threshold=…, event_retention_size=…))`。2.x から **モデル呼び出し前**（invocation 内）にも効く。2.8.0 で見積もりにツール呼び出し・応答の文字数が入った | **採用（本変更）** |
| 大きなツール結果の退避 | 大きい結果をファイルへ退避してポインタを返す / Read の offset・limit | 標準機能なし（`after_tool_callback` プラグインと Artifact で組める） | **実装（本変更）**: `ContextHarnessPlugin` + `read_tool_output` |
| ファイル操作ツール | `ls`/`read_file`/`write_file`/`edit_file`/`glob`/`grep` | `EnvironmentToolset`（read/write/edit/execute）、`ExecuteBashTool` | MCP の read/write/list/run/search（名前検索のみ）。本変更で出力上限と行範囲読み込みを追加 |
| 計画 / TODO | `write_todos` | `PlanReActPlanner` / `BuiltInPlanner` | `planner`（確認必須・状態に残らない） |
| サブエージェント（コンテキスト分離） | `task` ツール | `AgentTool`（子エージェントを独立コンテキストで実行し結果だけ返す） | A2A peer のみ。調査用の分離サブエージェントは無し |
| スキル（段階的開示） | Skills | `SkillToolset`（list/search/load skill、resource、script） | 独自 `SkillRegistry` + `enable_skill` |
| ツール失敗からの回復 | リトライ / 自己修正 | `ReflectAndRetryToolPlugin` | `on_tool_error` で観測値化のみ |
| プロンプトキャッシュ | Anthropic cache | `ContextCacheConfig`（2.8 で Anthropic のキャッシュブレークポイント対応） | 未使用 |
| 中断・再開 | チェックポイント | `ResumabilityConfig` | 未使用 |
| ベンダ製ハーネスの取り込み | — | `google.adk.labs.antigravity.AntigravityAgent`（Antigravity SDK の harness を ADK ノードとして包む。labs 扱い・Gemini 前提） | 不採用（マルチプロバイダ/ローカル LLM という憲章と合わない） |

結論: 「ADK を更新するだけで Deep Agents 相当になる」わけではない。ただし一番効く
**コンテキスト圧縮は ADK 標準で取り込める**。そこで ADK を 2.4 → 2.8 に上げて
（2.9.0 は依存の最低経過日数ゲート〔safe-chain〕に掛かるため見送り）、その上に DAK 側の
ハーネスを薄く載せた。

## 3. 実装したコンテキストハーネス（`agent/dak_agent/harness.py`）

`agent.py` は `root_agent` に加えて ADK の `App` を公開し、ADK の FastAPI アプリ（A2A を含む。
`agent/dak_agent/server.py` が `get_fast_api_app` で作る）は `app` のほうを優先して読み込む。安いものから順に 3 段で防ぎ、それでも超えたときの回復を 4 段目に置く。

1. **ツール出力の上限**（`ContextHarnessPlugin.after_tool_callback`）
   - 上限を超えた結果は先頭 70% と末尾 30% のプレビューに置き換え、全文は Artifact
     （`tool_output_<tool>_<call_id>.txt`）へ退避する。エージェントは
     `read_tool_output(artifact_name, offset, limit, pattern)` で続きを読んだり、
     正規表現で絞り込んだりできる。
   - MCP 結果で `structuredContent` が本文の複製になっている場合は落とす（全 MCP 呼び出しで
     約半分の節約になる）。
2. **ADK の token-threshold compaction**（`EventsCompactionConfig`）
   - 直近のプロンプトが窓の 60% を超えたら、モデルを呼ぶ前に古いイベントを要約して
     置き換える。直近 4 イベントはそのまま残し、関数呼び出しと応答のペアは ADK が壊さない。
   - 要約プロンプトは「元の依頼の原文、調べ済みのファイルと事実、決定事項、残作業」を
     残すよう DAK 用に調整した。
   - 要約は ADK 標準の `LlmEventSummarizer` ではなく、DAK の `BudgetedEventSummarizer`
     が作る。要約リクエスト自身を窓に収め（§5）、失敗しても例外を投げない。
3. **リクエストガード**（`ContextHarnessPlugin.before_model_callback`）
   - ADK は圧縮した要約を **model ロール**のメッセージとして差し込む。そのため元の依頼まで
     圧縮されると、リクエストにユーザーの発話が 1 つも残らない。llama.cpp の Qwen テンプレート
     （`--jinja`）はこれを `No user query found in messages` で拒否する（実機で確認。
     Anthropic も先頭がユーザーであることを要求する）。そこでユーザーの text ターンが
     無いときは、「以下の要約から作業を続けて」という短いユーザーターンを先頭に補う。
   - 組み立て後のリクエストが窓の 85% を超える場合、古いツール応答から順に
     `[elided …]` に差し替える。ADK はセッションの Content をそのまま使うため、
     オブジェクトは書き換えずに差し替える。
   - 見積もりは **CJK を 1 文字 = 1 トークン**で数える。単純な `len // 4` では日本語で
     3〜4 倍の過小評価になる。
4. **超過からの回復**（`ContextHarnessPlugin.on_model_error_callback`、#88）
   - 見積もりの誤差や巨大な system instruction で、モデルがそれでも窓超過のエラー
     （`is_context_overflow_error` が判定する各社の文面）を返したときだけ働く。
     予算を半分ずつ絞って 3 と同じ差し替えをかけ、同じモデルを直接呼び直す。
   - 呼び直しは `DAK_MODEL_ERROR_RETRY_ATTEMPTS` 回（既定 2）まで、1 回のコールバックの
     中で終える。回数を state に持たないので、次のターンに失敗が持ち越されない。
     呼び直しも 1 回のモデル呼び出しとして `max_llm_calls` に数える。
   - 尽きたら例外を投げず、先頭が `[CONTEXT_OVERFLOW]` の説明文（`CONTEXT_OVERFLOW_FAILURE_TEXT`）
     をそのターンの答えにする。invocation は失敗扱いにならず、同じセッションで次の入力を受け付ける。
   - 窓超過以外のエラーは扱わない（ADK が元の例外をそのまま送出する）。呼び直しの途中で
     窓超過以外のエラー（接続断・レート制限など）が出たら、説明文に置き換えずに送出する
     （ADK のプラグイン機構が `RuntimeError` で包み、元の例外は `__cause__` に残る）。

加えて:
- mcp-server: `read_file(path, offset, limit)`（行範囲）を追加し、`read_file`/`run_command` は
  `MCP_MAX_OUTPUT_CHARS`（既定 50K 文字）、`list_files`/`search_files` は `MCP_MAX_LIST_ENTRIES`
  （既定 500 件）で打ち切って、続きの取り方を示すようにした。
- `ModeManager`: トークン閾値トリガを削除した（圧縮はハーネスの担当）。モード切替は
  `switch_mode` 呼び出し時のみ行い、履歴は消さない。Meta-LLM プロンプトが存在しない
  `switch_mode(request_tool_list=True)` を指示していた不具合も直した。

### 設定（環境変数）

予算はすべてモデルのコンテキスト窓から算出する（`MODEL_CONTEXT_WINDOW`、無ければ
LiteLLM のモデルマップ、それも無ければ 128K）。

| 変数 | 既定 | 意味 |
|---|---|---|
| `DAK_CONTEXT_HARNESS` | `true` | `false` でハーネス全体を無効化 |
| `DAK_COMPACTION_THRESHOLD_RATIO` | `0.6` | 圧縮を始める窓占有率 |
| `DAK_COMPACTION_RETAIN_EVENTS` | `4` | 圧縮せずに残す直近イベント数 |
| `DAK_COMPACTION_INTERVAL` | `20` | sliding-window 圧縮の間隔（ユーザーターン数） |
| `DAK_COMPACTION_INPUT_RATIO` | `0.5` | 1 回の要約リクエストに入れる履歴の上限（窓占有率）。残りは要約の出力枠 |
| `DAK_REQUEST_BUDGET_RATIO` | `0.85` | 最終ガードの上限 |
| `DAK_MODEL_ERROR_RETRY_ATTEMPTS` | `2` | 窓超過のエラーを受けたときの呼び直しの回数（予算は毎回半分）。`0` で呼び直さずに説明文を返す |
| `DAK_TOOL_OUTPUT_MAX_CHARS` | 窓の 15%（2K〜40K 文字） | 1 回のツール結果の上限 |

目安: 8K 窓 → 圧縮 4,915 tok / ツール出力 2,000 文字。32K 窓 → 19,660 tok / 4,915 文字。
1M 窓 → ツール出力 40,000 文字。

### 検証

- `agent/tests/test_harness.py` の E2E テストは、実際の ADK `Runner` と台本どおりに動く
  モデル（8K 窓）で「1 invocation の中で巨大なツール結果を 6 回受け取る」状況を再現する。
  ハーネスなしでは 2 回目のリクエストが 38K トークンになって失敗し、ハーネスありでは
  リクエストが最大 6.2K トークン、圧縮 2 回で最後まで完走することを検証している。
  台本モデルは、ユーザーの発話が無いリクエストを Qwen テンプレートと同じように拒否するので、
  上記のガードも同じテストで検証される。
- 実機（llama-server + Qwen 27B, 32K）で元の依頼を再実行し、ツール出力の切り詰め
  （例: `read_file` 14,947 → 4,915 文字）と invocation 内での圧縮が働くことを確認した。
- 1 の不具合を再発させないため、`AdaptiveAgent` のテストは `session.events` を使う形に改めた。

### 計画と進捗（`write_todos` / `read_plan`、#87）

- 計画の各項目と進捗（`pending` / `in_progress` / `done`）はセッション state の `dak_todos` に置く。圧縮は履歴だけを要約に置き換え、state には触れないので、計画は残る。
- 指示は state から組み直され、最後に `# Current Plan` として計画が入る。組み直すのは呼び出しの始めと、`write_todos` の直後。そのため、長い呼び出しの途中で書いた計画も、次のモデル呼び出しから見える。
- 指示に入れる計画は、窓の 5%（1,000〜8,000 文字、`HarnessSettings.plan_chars`）までにする（#364）。超えたら、まず done の項目を件数の 1 行にまとめ、それでも超えたら項目の区切りで切って `read_plan` を案内する（先頭の未完了の項目は、長くても途中で切って必ず見せる）。state の計画と `read_plan` はこの上限で切り詰めない。ただし `read_plan` の結果も、ほかのツールと同じくツール出力の上限を受け、長ければ `read_tool_output` でページ送りする。
- `planner`（Ulysses Pact）は「これから使ってよいツール」を絞るもので、進捗は持たない。`write_todos` / `read_plan` は Pact で絞っていても常に呼べる。
- 検証: `test_harness.py::test_plan_survives_compaction`（圧縮後の最後のリクエストに計画がある）、`test_ulysses_pact.py::test_planner_restriction_does_not_block_write_todos_and_read_plan`。

### 承認の保留と reply（#100）

承認待ち（ツールの確認）と質問待ち（`ask_question`）を、どのクライアントからでも一覧して答えられる。
設計と根拠は `docs/design/approval-queue.md`。保留は ADK のセッションのイベントそのもので、別に保存しない。
入口は `agent/dak_agent/server.py`（ADK の FastAPI アプリに 3 本を足す。ADK の REST を同じプロセスの中で呼ぶ）。

| エンドポイント | 中身 |
|---|---|
| `GET /approvals?user_id=&session_id=[&app_name=dak_agent]` | 保留の一覧。1 件は `id`・`kind`（`approval` / `question`）・`tool_name`・`tool_args` / `hint`（承認）・`questions` / `context`（質問）・`requested_at`・`status`（`pending` / `timed_out`）・`session_id` |
| `POST /approvals/{id}/reply` | 本文 `{user_id, session_id, mode, reason}`（承認）か `{user_id, session_id, answer}`（質問）。セッションを再開して ADK のイベントを返す。保留に無い `id` は `404`、`mode` の誤りは `422` |
| `GET /approvals/stream?user_id=&session_id=` | 同じ一覧を server-sent events で。新しい保留に `approval.asked`、期限が切れたら `approval.timed_out`（一覧には残る）、一覧から消えたら `approval.replied`（2 秒ごとに読む） |

| 答え | モデルに届く Observation |
|---|---|
| `once` | ツールの実行結果 |
| `always` | ツールの実行結果。`PermissionPlugin` が `payload.mode: "always"` を読んで継続許可を保存する（#178） |
| `reject` + `reason` | `{"observation": "denied_by_user", "reason": ...}`（`AdaptiveAgent._restore_reject_reason`） |
| `timed_out` | `{"observation": "timed_out"}`。`DAK_APPROVAL_TIMEOUT_SECONDS`（既定 900）を過ぎた承認に reply すると、その答えの代わりに流し、HTTP は `409`。一覧（GET / SSE）は示すだけで消費しない |

MRTR（MCP 2026-07-28、SEP-2322）との対応: 保留の 1 件 ↔ `InputRequiredResult`（`resultType: "input_required"`）、`id` ↔ `inputRequests` のキー、
`once` / `always` ↔ `ElicitResult.action: "accept"`、`reject` ↔ `"decline"`、`timed_out` ↔ `"cancel"`、質問の `answer` ↔ `accept` の `content`。
`requestState`（状態をクライアントが運ぶ）と、mcp-server が返す `input_required` の透過は未対応（表の全体は設計文書の 5）。

確認を求めるのは、`PermissionPlugin` の規則が `ask` になる MCP のツール（既定の MCP サーバの書き込み・コマンドなど。`agent/dak_agent/permission.py` の `DEFAULT_RULES`）と、`DAK_PLANNER_REQUIRE_CONFIRMATION=true` のときの `planner`。どちらも同じ `adk_request_confirmation` の形なので、一覧と reply は区別しない。拒否は `PermissionPlugin` も ADK と同じ固定文言で返すので、理由は同じ `_restore_reject_reason` でモデルに届く。

## 4. 残りのギャップとバックログ（優先度順）

各項目は GitHub Issue 化して [DAK Project #7](https://github.com/users/teeppp/projects/7) で管理している。

> 2026-09 追記: Codex CLI / OpenCode / Gemini CLI / Goose / Claude Code ほかの OSS ハーネスを実装レベルで
> 調査し、追加のバックログ（#99〜#118、Epic #119）を起票した。調査本文と横断比較は
> `docs/comparison/harness-survey-2026-09/README.md`。下表の #85〜#94 にも設計参照をコメントで追記済み。

| 優先 | 項目 | 狙い | 関連 |
|---|---|---|---|
| P1 | **調査用サブエージェント（`AgentTool`）** | 「リポジトリを読んで要約」を子エージェントに任せ、親のコンテキストには結論だけを残す（Deep Agents の `task`、Claude Code の Explore 相当）。長い調査タスクで最も効く | #85 |
| P1 | **内容検索ツール（grep）と行番号付き読み込み** | 今の `search_files` はファイル名しか検索できず、中身を探すにはファイル全体を読むしかない。`grep(pattern, path, glob)` と `edit_file`（文字列置換）を足すか、ADK `EnvironmentToolset` への移行を検討 | #86, #16, #20 |
| ~~P1~~ | ~~**TODO ツール（セッション state に保存）**~~ | **済み（#87）**: `write_todos` / `read_plan`。上の「計画と進捗」 | #87, #21 |
| ~~P2~~ | ~~**コンテキスト超過からの回復**~~ | **済み（#88）**: 圧縮側は §5、モデル呼び出し側は §3 の 4（予算を絞って有限回呼び直し、尽きたら説明文で終える） | #88 |
| P2 | **窓サイズの自動検出** | llama-server の `/props`（`n_ctx`）から窓を取る。compose 既定の 8192 と実サーバーの 32768 のようなずれを防ぐ | #89 |
| P2 | **`SkillToolset` への移行** | 独自の `SkillRegistry`/`enable_skill` を ADK 標準（Agent Skills 仕様・段階的開示・リソース読み込み）に寄せ、保守コストを下げる | #90, #81 |
| P2 | **ツール失敗の自己修正** | `ReflectAndRetryToolPlugin` を試す | #91 |
| P2 | **モード切替の整理** | 圧縮とスキルで役割の多くが代替されたので、Meta-LLM によるモード切替を残すかどうかを評価で判断する（動的ツール削減 #81 と合わせて検討） | #92, #81 |
| P3 | **長時間タスクの評価** | nightly-eval に「リポジトリ調査」系の長いゴールデンシナリオを加え、窓超過率・圧縮回数・トークン数を Langfuse の指標で追う | #93, #5, #71 |
| P3 | **プロンプトキャッシュ** | `ContextCacheConfig`（Gemini/Anthropic）でコストと遅延を下げる | #94 |
| P3 | **サンドボックス** | ファイル・コマンド系ツールの分離 | #20, #31, #43, #80 |

## 5. 2 度目の発端: 圧縮の要約リクエスト自身が窓を超えた（2026-09-14）

§3 のハーネスを入れた後、外部のクライアントから llama.cpp（Qwen3 27B, `n_ctx=32768`）の DAK に
送ったタスクが次のエラーで止まり、「つづけて」を送っても同じエラーで即死するようになった。

```
litellm.ContextWindowExceededError: request (50848 tokens) exceeds the available
context size (32768 tokens)          ← 翌日の再送では 52152 tokens
```

### 何が起きていたか

agent ログのスタックトレース、ADK セッション DB（Postgres の `events`）、クライアント側の
実行ログを突き合わせた結果:

| # | 事実 | 出典 |
|---|------|------|
| 1 | 例外は **モデル呼び出しではなく圧縮の要約呼び出し**（`compaction.py → LlmEventSummarizer.maybe_summarize_events`）から出ている | agent ログ |
| 2 | 直前のモデル呼び出しのプロンプトは **20,439 トークン**（窓の 62%、ガードの予算内）。それを圧縮するための要約リクエストが **50,848 トークン** | セッション DB の `usage_metadata` と llama.cpp の 400 応答 |
| 3 | 前回の圧縮（14:01）以降の 20 イベント（seed 込み）に含まれる **思考（thought）が約 23,000 文字**。しかもストリーミングで **約 5,600 個の数文字の thought パート**として保存されており、ADK の summarizer はその 1 つ 1 つを `dak_agent (thought): ` 付きの行にするので、要約プロンプトは 176,000 文字になった。ツール応答は ADK が 2,000 文字で切るが、思考と本文は無制限 | セッション DB / 再現スクリプト |
| 4 | 思考はモデルのプロンプトにも入る（LiteLLM が `reasoning_content` として送り返し、Qwen3 のテンプレートは過去ターンの分も `<think>` として描画する。llama-server の `/apply-template` で確認）。ただしモデル側は思考 1 パート = 1 行ではなく本文として連結されるため、要約側だけが断片ごとのプレフィックスで 2.5 倍に膨れた | `lite_llm.py` / `/apply-template` |
| 5 | 要約呼び出しは ADK が summarizer の `llm` を直接叩くため、`before_model_callback`（§3 のリクエストガード）を **通らない** | ADK `llm_event_summarizer.py` |
| 6 | 圧縮のトリガは「最後に観測したプロンプトが閾値以上」で、失敗しても何も変わらないため **次のターンでも同じ圧縮が同じ入力で走り、同じ例外で死ぬ**。セッションが恒久的に詰む | ADK `compaction.py` |

要するに、§3 の 3 層は「モデルへのリクエスト」を守っていたが、「要約のためのリクエスト」は
誰も守っておらず、推論モデル（思考を大量に出す）でそこが先に溢れた。

### 直したこと（`BudgetedEventSummarizer`）

ADK の `LlmEventSummarizer` を継承し、要約リクエストを自分で予算内に収める。

1. **履歴エントリの結合と上限**: 同じイベント内で連続する思考（本文）パートは 1 エントリに結合する。
   その上で、思考・ツール呼び出し・ツール応答は `compaction_entry_chars`
   （窓の 5%、400〜2,000 文字）、ユーザー発話・モデルの本文・前回の要約（seed）はその 4 倍まで。
2. **予算への当てはめ**: 描画した履歴が `DAK_COMPACTION_INPUT_RATIO`（既定 0.5）× 窓を
   超えるなら、嵩張るエントリの上限を半分ずつ縮め（下限 200 文字）、次に密なエントリを縮め、
   それでも超えるなら **古い嵩張るエントリから落とし**、次に古い密なエントリを落とす。先頭の密な
   エントリ（元の依頼か前回の要約）は落とさない。
3. **モデルに拒否された場合**: 窓超過のエラーなら予算を半分にして再試行（3 回まで。縮められなく
   なったら即打ち切り）。それでも拒否されたら、また要約が思考だけで本文が無かったら、当てはめ済みの
   抜粋そのものを要約として圧縮イベントにする。
   **圧縮が原因でターンが失敗することはなくなる**。
4. **それ以外のモデル障害**（接続断など）: ログを出して今回の圧縮を **スキップ**（`None`）。
   次のモデル呼び出しはリクエストガードが窓内に収め、障害が続くならそこで見える形で失敗する。
5. 要約プロンプトに「数百語に収める」を追加（6.8 tok/s の環境で 3,500 トークンの要約を
   9 分かけて書いていた）。
6. リクエストガードは、予算超過時に **古い model ターンの署名なし思考パートを最初に落とす**
   （署名付きの思考は Anthropic/Gemini が返却を要求する不透明な状態なので残す）。それまでガードは
   思考を数えるだけで削れず、推論モデルではツール応答を消しても足りなかった。

### 復旧の仕方

この変更が入った agent では、詰んでいたセッションに次のメッセージを送るだけでよい。
最初のモデル呼び出しの前に圧縮が走り、要約が窓内に収まってセッションが前に進む
。実機の詰んだセッション `7bcbddac…` の 81 イベントを ADK の選択ロジックと本 summarizer に通して
`llama-server /tokenize` で数えたところ、ADK 標準の要約プロンプトは **52,152 トークン**（エラーの数値と一致）、
本 summarizer では **8,882 トークン**（予算 16K、32K 窓）に収まった。回避策として旧バージョンでは
`DAK_CONTEXT_HARNESS=false` で圧縮ごと止められるが、その場合リクエストガードも消えるので勧めない。

### 検証

- `agent/tests/test_harness.py`
  - `test_adk_summarizer_overflows_on_a_reasoning_model`: 台本モデルが毎ステップ約 3.4K 文字の
    日本語の思考を出す状況で、ADK 標準の summarizer だと **モデルのリクエストは窓内なのに
    要約リクエストが窓を超えて run が死ぬ**ことを再現する（本件の縮小版）。
  - `test_budgeted_summarizer_keeps_compaction_inside_the_window`: 同じ状況で
    `BudgetedEventSummarizer` なら要約・モデルのリクエストとも窓内で完走する。
  - `TestBudgetedEventSummarizer`: 予算への当てはめ（嵩張るものから落とし、seed は残す）、
    窓超過エラーでの再試行と抜粋へのフォールバック、その他エラーでのスキップ。
