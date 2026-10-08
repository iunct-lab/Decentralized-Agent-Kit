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
| サブエージェント（コンテキスト分離） | `task` ツール | `AgentTool`（子エージェントを独立コンテキストで実行し結果だけ返す） | A2A peer に加え、調査用の `AgentTool`（`dak_explorer`、#85）を実装。§3 の「調査サブエージェント」 |
| スキル（段階的開示） | Skills | `SkillToolset`（list/search/load skill、resource、script） | 独自 `SkillRegistry` + `enable_skill` |
| ツール失敗からの回復 | リトライ / 自己修正 | `ReflectAndRetryToolPlugin` | `on_tool_error` で観測値化 + §3 の 0（繰り返しのガード）。未知のツール名は `{"observation": "unknown_tool", "candidates"}`（`difflib` で近い名前を最大 3 つ、#185）。ReflectAndRetry は不採用（`docs/design/reflect_retry_plugin.md`、#91） |
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
その手前で、止まらないツールの呼び出しそのものを 0 段目で止める。

0. **ツール呼び出しのガード**（`ContextHarnessPlugin.before_tool_callback`、#170）
   - 小型モデルは同じツールを同じ引数で呼び続けやすい。次のどれかに当たったら、ツールを
     実行せずに理由付きの Observation（辞書）をツールの結果として返す。例外は投げない。
     - 同じ呼び出し（ツール名 + 引数の SHA-256）が `DAK_MAX_REPEATED_TOOL_CALLS`（既定 3）回を
       超えて続いた → `{"observation": "repeated_call", "tool", "count", "hint"}`
     - 1 invocation のツール呼び出しが `DAK_MAX_TOOL_CALLS`（既定 40）回を超えた → `step_limit_exceeded`
     - invocation の最初のツール呼び出しから `DAK_MAX_WALL_SECONDS`（既定 300）秒を超えた → `wall_time_exceeded`
   - `PermissionPlugin` が先に答えた呼び出し（拒否・承認待ち）は ADK がこの `before_tool_callback` を飛ばすので、
     `after_tool_callback`（ADK はすべてのプラグインで実行する）で数える。拒否された呼び出しも連続を切り、上限に数える。
   - カウンタは invocation ごとに `temp:dak_tool_guard` に持つ（ADK の `temp:` state は保存されないので、
     セッションに溜まらず、次の invocation は 0 から数える）。
   - 反復ではない連続した失敗（引数のスキーマ違反など）は、検査する側が
     `note_argument_violation(tool_context, invocation_id)` で数える。同じ上限
     （`DAK_MAX_REPEATED_TOOL_CALLS`）を超えて続くと `True` を返すので、呼び出し側は訂正のヒントの代わりに
     止める Observation を返す。正常に実行できたら `note_call_success` で数え直す（#171）。
   - 圧縮を何度も挟む長い invocation でも、カウンタは state にあって圧縮では消えないので、呼び出し数の上限で
     有限回に止まる。`test_tool_loop_stops_at_step_limit_without_raising` が上限より 3 回多く呼び続ける台本モデルで、
     圧縮 2 回以上のあとに `step_limit_exceeded` が返り、例外が出ないことを確かめている。
   - ツール定義を次のモデル呼び出しから外すことはしない（PBI #99 の決定ログ）。

1. **ツール出力の上限**（`ContextHarnessPlugin.after_tool_callback`）
   - 上限を超えた結果は先頭 70% と末尾 30% のプレビューに置き換え、全文は Artifact
     （`tool_output_<tool>_<call_id>.txt`）へ退避する。エージェントは
     `read_tool_output(artifact_name, offset, limit, pattern)` で続きを読んだり、
     正規表現で絞り込んだりできる。
   - プレビューの先頭には `[truncated: original N chars / L lines; kept head H chars + tail T chars; artifact=<name>]`
     の見出しを付け、間の省略は `... [X chars / Y lines omitted] ...` と書く（退避できなかったときは
     `artifact=` を付けない）。モデルが切り詰めを全出力と取り違えないため。
   - A2A peer が設定されていて（`ENABLE_A2A_CONSUMER=true` で peer を読み込めた）、その呼び出しで
     `transfer_to_agent` が使える（`dak:tools` で絞っていない、または名指ししている）ときだけ、`hint` に
     「広い読み取りは `transfer_to_agent` で peer への委譲を検討せよ。peer はこの出力も artifact も見えないので、
     渡すのはデータではなく問い」の 1 文を足す。それ以外と、全文を退避できなかったときは再読取（`read_tool_output`）
     または呼び出しを絞る方法だけを示す。
   - MCP 結果で `structuredContent` が本文の複製になっている場合は落とす（全 MCP 呼び出しで
     約半分の節約になる）。
2. **ADK の token-threshold compaction**（`EventsCompactionConfig`）
   - 直近のプロンプトが窓の 60% を超えたら、モデルを呼ぶ前に古いイベントを要約して
     置き換える。直近 4 イベントはそのまま残し、関数呼び出しと応答のペアは ADK が壊さない。
   - 要約プロンプトは「元の依頼の原文、調べ済みのファイルと事実、決定事項、残作業」を
     残すよう DAK 用に調整した。2 回目以降の圧縮では、ADK が前回の要約を要約対象の先頭に入れるので、
     要約は作り直しではなく前回の要約の更新になる（`test_second_compaction_carries_forward_the_first_summary`、#103）。
   - 要約は ADK 標準の `LlmEventSummarizer` ではなく、DAK の `BudgetedEventSummarizer`
     が作る。要約リクエスト自身を窓に収め（§5）、失敗しても例外を投げない。
3. **リクエストガード**（`ContextHarnessPlugin.before_model_callback`）
   - ADK は圧縮した要約を **model ロール**のメッセージとして差し込む。そのため元の依頼まで
     圧縮されると、リクエストにユーザーの発話が 1 つも残らない。llama.cpp の Qwen テンプレート
     （`--jinja`）はこれを `No user query found in messages` で拒否する（実機で確認。
     Anthropic も先頭がユーザーであることを要求する）。そこでユーザーの text ターンが
     無いときは、「以下の要約から作業を続けて」という短いユーザーターンを先頭に補う。
   - 毎回、セッションの圧縮イベントを数え直して state の `dak_compaction_count` に書く（#103）。
     `DAK_COMPACTION_WARNING_COUNT`（既定 3）回を超えたら `dak_recommend_new_session` を `true` にし、
     警告ログを 1 回出す（圧縮を重ねるほど答えの精度が落ちるので、新しいセッションを勧める印）。
     クライアントへの表示はまだ無く、state に書くところまで。
   - 次に、古いツール結果を要約なしで剪定する（`prune_old_tool_results`、#102）。直近
     `DAK_PRUNE_PROTECT_USER_TURNS`（既定 2）件のユーザーターン（利用者のテキストを持つターン。
     ツール結果は数えない）以降は触らず、それより前のツール応答を新しい方から数えて
     `DAK_PRUNE_PROTECT_TOKENS`（既定: 窓の 20%）を超えた古い側を
     `[cleared; read_tool_output('<artifact>') で再取得可]` に差し替える。全文は 1 の Artifact
     （無ければここで保存する）にあり、呼び出しと応答の対（id と name）は保つ。剪定できる量が
     `DAK_PRUNE_MINIMUM_TOKENS`（既定 512）未満なら何もしない。
   - 剪定は 2 の圧縮の前段として効く。圧縮するかは直前のリクエストの大きさで決まり、そのリクエストは
     剪定した後のものなので、剪定だけで閾値の下に収まるあいだは要約が 0 回で済む。1 ターンの中の長い
     ツールループは保護の範囲なので剪定されず、従来どおり圧縮が受け持つ。
   - 組み立て後のリクエストが窓の 85% を超える場合、古いツール応答から順に
     `[elided …]` に差し替える。ADK はセッションの Content をそのまま使うため、
     オブジェクトは書き換えずに差し替える。直近の tail（`DAK_TAIL_RESERVE_RATIO`、既定: 窓の 20%）は
     原文のまま残す。予算の切れ目がターンの途中なら、そのターンの始まり（利用者のテキスト）まで
     広げる（`_tail_keep_count`、#103）ので、ターンを途中で切らない。ただし広げた tail がリクエストの
     予算（固定部分を除く）を超えるとき（1 ターンの長いツールループ）は広げず、予算の切れ目で切る
     （ガードがそのターンの古い側を縮められるように）。
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
  （既定 500 件）で打ち切って、続きの取り方を示すようにした。`read_file` は行の境界で切り、続きを読む
  `offset`（次の行番号）をそのまま示す（1 行だけで上限を超えるときは、その行の先頭を見せて次の行から続ける。その行の残りは `read_file` では
  読めないので、ヒントにその量を書き、`run_command` で絞るよう示す）。
  `run_command` は stdout / stderr それぞれ先頭 30% と末尾 70% を残し、間で落とした文字数を示す
  （失敗の理由や要約は出力の末尾に出ることが多いため）。
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
| `DAK_COMPACTION_WARNING_COUNT` | `3` | 圧縮がこの回数を超えたら `dak_recommend_new_session` を立てる |
| `DAK_TAIL_RESERVE_RATIO` | `0.2` | 最終ガードが原文のまま残す直近 tail（窓占有率、最低 256 トークン。ユーザーターンの境界から） |
| `DAK_MODEL_ERROR_RETRY_ATTEMPTS` | `2` | 窓超過のエラーを受けたときの呼び直しの回数（予算は毎回半分）。`0` で呼び直さずに説明文を返す |
| `DAK_TOOL_OUTPUT_MAX_CHARS` | 窓の 15%（2K〜40K 文字） | 1 回のツール結果の上限 |
| `DAK_PRUNE_PROTECT_USER_TURNS` | `2` | 剪定しない直近のユーザーターン数 |
| `DAK_PRUNE_PROTECT_TOKENS` | 窓の 20% | 保護したターンより前で、剪定せずに残すツール結果の量（新しい方から） |
| `DAK_PRUNE_MINIMUM_TOKENS` | `512` | 剪定できる量がこれ未満なら剪定しない |
| `DAK_MAX_REPEATED_TOOL_CALLS` | `3` | 同じ呼び出し（ツール + 引数）を続けて実行してよい回数。超えると `repeated_call` |
| `DAK_MAX_TOOL_CALLS` | `40` | 1 invocation で実行してよいツール呼び出しの数。超えると `step_limit_exceeded` |
| `DAK_MAX_WALL_SECONDS` | `300` | 最初のツール呼び出しからこの秒数を過ぎたら、以後のツールを `wall_time_exceeded` で止める |

目安: 8K 窓 → 圧縮 4,915 tok / ツール出力 2,000 文字。32K 窓 → 19,660 tok / 4,915 文字。
1M 窓 → ツール出力 40,000 文字。

### 検証

- `agent/tests/test_harness.py` の E2E テストは、実際の ADK `Runner` と台本どおりに動く
  モデル（8K 窓）で「1 invocation の中で巨大なツール結果を 6 回受け取る」状況を再現する。
  ハーネスなしでは 2 回目のリクエストが 38K トークンになって失敗し、ハーネスありでは
  リクエストが最大 6.2K トークン、圧縮 2 回で最後まで完走することを検証している。
  台本モデルは、ユーザーの発話が無いリクエストを Qwen テンプレートと同じように拒否するので、
  上記のガードも同じテストで検証される。
- 剪定と圧縮の順序は、同じ台本モデルに複数ターンを送る E2E で固定している。
  `test_prune_alone_completes_the_task_with_zero_compactions`（1 ターン 1 回のツール結果を 4 ターン。
  剪定ありは要約 0 回、剪定を止めると要約が走る）と
  `test_prune_then_compaction_when_pruning_alone_is_not_enough`（3 ターン目に 3 回。剪定したうえで
  なお閾値を超え、要約も走る）。
- 実機（llama-server + Qwen 27B, 32K）で元の依頼を再実行し、ツール出力の切り詰め
  （例: `read_file` 14,947 → 4,915 文字）と invocation 内での圧縮が働くことを確認した。
- 1 の不具合を再発させないため、`AdaptiveAgent` のテストは `session.events` を使う形に改めた。

### 計画と進捗（`write_todos` / `read_plan`、#87）

- 計画の各項目と進捗（`pending` / `in_progress` / `done`）はセッション state の `dak_todos` に置く。圧縮は履歴だけを要約に置き換え、state には触れないので、計画は残る。
- 指示は state から組み直され、最後に `# Current Plan` として計画が入る。組み直すのは呼び出しの始めと、`write_todos` の直後。そのため、長い呼び出しの途中で書いた計画も、次のモデル呼び出しから見える。
- 指示に入れる計画は、窓の 5%（1,000〜8,000 文字、`HarnessSettings.plan_chars`）までにする（#364）。超えたら、まず done の項目を件数の 1 行にまとめ、それでも超えたら項目の区切りで切って `read_plan` を案内する（先頭の未完了の項目は、長くても途中で切って必ず見せる）。state の計画と `read_plan` はこの上限で切り詰めない。ただし `read_plan` の結果も、ほかのツールと同じくツール出力の上限を受け、長ければ `read_tool_output` でページ送りする。
- `planner`（Ulysses Pact）は「これから使ってよいツール」を絞るもので、進捗は持たない。`write_todos` / `read_plan` は Pact で絞っていても常に呼べる。
- 検証: `test_harness.py::test_plan_survives_compaction`（圧縮後の最後のリクエストに計画がある）、`test_ulysses_pact.py::test_planner_restriction_does_not_block_write_todos_and_read_plan`。

### 元の依頼（#103）

- セッションの最初のユーザー発話（テキストのパートを改行でつないだもの）を、最初の呼び出しの始めに state の `dak_original_request` へ一度だけ保存する。後のターンで上書きしない。
- 指示は計画と同じく state から組み直され、`# Current Plan` の後に `# Original Request` として入る。要約の `User request` 節はモデルの出来に左右されるが、こちらは圧縮の出来によらず残る。
- 計画と同じく、利用者が書いた文なので ADK の `{var}` 置換を通さない。長さは計画と同じ上限（`HarnessSettings.plan_chars`）で切り、切ったら `[truncated — call read_original_request for the full text]` を付ける。全文は組み込みツール `read_original_request` が state から返す（`read_plan` と同じく Pact で絞っていても呼べる）。`dak:instruction` を渡した呼び出しには入れない（指示全体を置き換えるため）。
- 検証: `test_adaptive_agent.py::test_original_request_reaches_later_turns_verbatim`（2 ターン目の指示に 1 ターン目の発話がそのまま入る）。

### 引き継ぎ情報（`write_handoff`、#114）

- 長い作業を文脈のリセット後に続けるための引き継ぎ情報（目的・完了・決定・次の作業・ファイル・未解決事項）を、組み込みツール `write_handoff` が state の `dak_handoff` に置く。
- 指示は計画と同じく state から組み直され、`# Original Request` の後に `# Handoff` として入る。組み直すのは呼び出しの始めと、`write_handoff` の直後。モデルが書いた文なので `{var}` 置換を通さない。長さは計画と同じ上限（`HarnessSettings.plan_chars`）で切り、切ったら `[truncated — call read_handoff for the full text]` を付ける。全文は組み込みツール `read_handoff` が state から返す（`write_handoff` と `read_handoff` は Pact で絞っていても呼べる）。`dak:instruction` を渡した呼び出しには入れない。
- 組み直しは呼び出しごとに state からなので、どこから来た呼び出しかを区別しない。同じセッションを A2A（ADK の `A2aAgentExecutor`。A2A の context がセッションになる）や、再起動した別のプロセスから続けても、保存済みの handoff が入る。
- リセットの材料は `harness.build_reset_compaction(events, handoff_text, original_request_text)`。渡したイベントの範囲全体を覆う ADK の `EventCompaction` を返し、中身は `User request: <元の依頼>` と handoff の文だけ。これを `actions.compaction` に持つイベントをセッションに足すと、以降のリクエストはその範囲の生の履歴（ツールの結果も）の代わりにこの 2 つから組まれる。新しいリセットの仕組みは作らず、自動の圧縮と同じ ADK の圧縮イベントを使う。呼び出す口（`new_context` ツール）は #113。
- 検証: `test_adaptive_agent.py::test_saved_handoff_reaches_a_resumed_session_in_a_new_process_verbatim`、`test_adaptive_agent.py::test_saved_handoff_reaches_a_turn_that_comes_over_a2a`、`test_adaptive_agent.py::test_handoff_written_mid_invocation_reaches_the_next_model_call`、`test_harness.py::test_reset_compaction_lets_the_scripted_task_complete_from_handoff_alone`、`test_harness.py::test_build_reset_compaction_covers_full_range`。

### プロジェクトの指示（`get_project_instructions`、#115）

- 作業ツリーの `AGENTS.md` / `CLAUDE.md` / `CONTEXT.md` を、mcp-server のツール `get_project_instructions(path=".")` が読む。ワークスペースのルート（`/projects`）から `path` へ降りながら、各ディレクトリで `AGENTS.md` → `CLAUDE.md` → `CONTEXT.md` の最初の 1 件を採り、`--- <dir>/<file> ---` の区切りでルート側から連結する（深い方が後ろ＝優先）。合計 `MCP_INSTRUCTIONS_MAX_BYTES`（既定 32768 バイト）で切り、`[truncated: …]` を付ける。
- 信頼: `MCP_TRUSTED_WORKSPACE_PREFIXES`（`/projects` からの相対、`:` 区切り、既定 `.` = マウント全体）の外のパス、ワークスペースの外（`..`、絶対パス、外へ向くシンボリックリンク）は何も開かず `[not read: …]` を返す。プレフィックスの外にある祖先ディレクトリの指示ファイルも読まない。読むのはいつもサーバの `/projects` で、`SANDBOX_MODE` のセッションのワークスペース（モデルが書いたファイル）は読まない。
- agent は毎 invocation の始め（`before_agent_callback` の `_restore_session_config`、指示を組む前）に既定の MCP サーバのこのツールを 1 回だけ自分で呼び（モデルには選ばせない）、結果を state の `dak_project_instructions` に置く。`[not read` で始まる結果や空なら `None` にして消す（ADK の `State` に削除は無い）。呼び出しの失敗やエラーの結果（`isError`、`Error reading project instructions`）では前の値を残す（invocation は落とさない）。値が変わったときだけ書くので、同じ指示で毎ターン state の差分は増えない。ツールの無い古い mcp-server、既定の MCP サーバを持たない agent、拒否される呼び出し（許されない `dak:model` など）、`dak:instruction` を渡した呼び出しでは呼ばない。コストは invocation ごとに MCP の往復 2 回（ツール一覧と呼び出し）。
- 指示には計画の前に `# Project Instructions` として入る。ファイルの文なので `{var}` 置換を通さない。長さは計画と同じ上限（`HarnessSettings.plan_chars`）で切り、切ったら `[truncated — call get_project_instructions for the full text]` を付ける。`dak:instruction` を渡した呼び出しには入れない。
- 検証: `mcp-server/tests/test_project_instructions.py`、`test_adaptive_agent.py -k project_instructions`。

### 調査サブエージェント（`dak_explorer`、#85）

- 広い調査（多数のファイルの走査・要約）を、読み取り専用のサブエージェント `dak_explorer` に委譲できる（`agent/dak_agent/explorer.py`）。ADK の `AgentTool` で包んで `root_agent` のツールに入れ、指示の末尾に「広い調査は `dak_explorer` に委譲する」の 1 文を足す。呼び出し元が `dak:tools` でツールを絞ったときは、名指ししたときだけ使える。
- `dak_explorer` は同じプロセスの中で、親がその呼び出しで使うモデル（`dak:model` で選んだモデルも）で動き、既定の MCP サーバの読み取り専用ツール（`read_file` / `list_files` / `search_files` / `grep` / `deep_think`）だけを持つ。`AgentTool` が別のセッションで走らせるので、ツールの生出力は親のセッションにもモデル要求にも入らず、最終回答だけがツール結果になる。
- A2A peer（`RemoteA2aAgent`、`agent/dak_agent/a2a_peer_manager.py`）は別のプロセスの別のエージェントで、`transfer_to_agent` で会話ごと渡し、書き込みも含めて自分のツールと設定で自律的に動く。
- 使い分け: 同じ会話の中で一時的に調査だけを分離したいときは `dak_explorer`、別のエージェントに任せて独立して動かしたいとき（そのエージェントにしか無いツールや権限が要るときも）は A2A peer。
- 制限: 確認の要る読み取り（既定では `*.env`）はサブエージェントの中では承認できず、実行されない（親が自分で読めばふつうの承認に乗る。#535）。隔離環境（`SANDBOX_MODE` が off 以外）では親と別の作業場所を見る（#527）。
- 検証: `agent/tests/test_explorer.py`（親のモデル要求とセッションに生出力が入らず結論だけが入る、読み取り専用のツールだけ、親の呼び出しのモデルで動く）。

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

### hooks（PreToolUse / PostToolUse / Stop、#107）

運用者がコードを触らずに、ツール呼び出しを監査・拒否・書き換えできる口。既定は無し（`DAK_HOOKS` を設定したときだけ起動する）。
実装は `agent/dak_agent/hooks.py`（読み込みと実行）と `ContextHarnessPlugin`（結線）。

`DAK_HOOKS` は JSON の配列（TOML は読まない）。1 件の形:

| キー | 必須 | 意味 |
|---|---|---|
| `event` | ○ | `PreToolUse` / `PostToolUse` / `Stop` |
| `type` | ○ | `command`（シェルで実行）/ `http`（POST） |
| `command` / `url` | type に応じて ○ | 実行するコマンド / 送り先の URL |
| `timeout` | | 秒（正の数、既定 30）。過ぎたらエラー扱いで、command はプロセスグループごと止める |
| `if` | | ツール名の glob（例 `run_*`）。無ければ全ツール。Stop には効かない（ツールが無いので常に起動する） |

```json
[{"event": "PreToolUse", "type": "command", "command": "python3 /opt/hooks/no_rm.py", "if": "run_command"}]
```

不正な要素は警告して捨てる（ほかの要素は効く）。同じイベントの hook は書いた順に 1 本ずつ実行し、最初に拒否か書き換えを返した hook で決まる。

入出力は Claude Code の hooks と同じ契約なので、Claude Code 用に書いた hook をそのまま使える
（`agent/tests/fixtures/precommit_style_hook.py` で固定）。

- 入力（command は stdin、http は POST 本文の JSON）: `hook_event_name`・`session_id`・`cwd`・`permission_mode`・`tool_name`・`tool_input`・`tool_use_id`。PostToolUse には `tool_response` も付く
- 出力: command の exit 2 は拒否（stderr が理由）。exit 0 なら stdout の `hookSpecificOutput`（`permissionDecision`・`permissionDecisionReason`・`updatedInput`・`updatedToolOutput`）を読む。トップレベルの `{"decision": "block", "reason": ...}` も拒否として読む。http は 2xx の本文を同じ規則で読む。そのほかの exit コード・接続失敗・タイムアウトは警告を出して次の hook へ進む（ツールは止めない）
- `permissionDecision: "ask"` は拒否にする（hook の呼び出しの中で利用者に聞く手段が無いため）

| 結果 | モデルに届く Observation |
|---|---|
| PreToolUse の拒否 | `{"observation": "blocked_by_hook", "reason": ..., "hook_event": "PreToolUse"}`（ツールは動かない） |
| PreToolUse の `updatedInput` | `{"observation": "hook_rewrote_input", "original_args": ..., "updated_args": ..., "result": ...}`（書き換えた引数でツールを実行する。`result` はツールの結果にツール出力の上限をかけたもの） |
| PostToolUse の拒否 | `{"observation": "blocked_by_hook", "reason": ..., "hook_event": "PostToolUse"}`（ツールの結果は渡さない。PreToolUse が引数を書き換えていたら `original_args` / `updated_args` も載せる） |
| PostToolUse の `updatedToolOutput` | `{"observation": "hook_rewrote_output", "result": ...}`（元の出力は載せない。`result` は書き換え後の出力にツール出力の上限をかけたもの） |

- PreToolUse は実行ガード（反復・回数・時間）のあとに起動する。ガードが止めた呼び出しでは起動しない
- `updatedInput` は ADK が渡した引数をその場で書き換えるので、ツールは ADK の通常の経路で動く（エラーは `on_tool_error` を通る）。PostToolUse には書き換え後の引数が届く
- 書き換えた引数は `PermissionPlugin` で評価し直さない（`PermissionPlugin` は書き換えの前に元の引数で評価済み）。hook は運用者の設定で、`agent_config.yaml` の規則と同じく信頼する（Claude Code でも hook の `allow` は権限の確認を飛ばす）。規則で止めたい入力へ書き換える hook を置かない
- PostToolUse は、ツールが動いて成功した呼び出しだけで起動する（Claude Code の PostToolUse と同じく失敗は対象外）。起動しないのは、`PermissionPlugin` が拒否・承認待ちにした呼び出し、ガード・PreToolUse が止めた呼び出し、ツールが例外で終わった呼び出し、MCP のツールが `isError: true` を返した呼び出し、ADK の確認（`require_confirmation`、`DAK_PLANNER_REQUIRE_CONFIRMATION=true` の `planner`）が確認待ち・拒否で答えた呼び出し、`read_tool_output`（ハーネス自身のページ送り）
- 確認待ち・拒否で答えた呼び出しには書き換えの包みもかけない（拒否の理由はエージェントの `_restore_reject_reason` が返す。確認のあとの呼び直しでは PreToolUse がもう一度書き換える）。`read_tool_output` の引数の書き換えは、ツール出力の上限をかけずに `hook_rewrote_input` で包む
- **Stop は監査・通知専用で、止められない。** ADK の `after_run_callback` は戻り値が `None` 固定で、Claude Code の Stop hook のように拒否してエージェントを続けさせることができない。拒否やエラーが返っても警告をログに出すだけ。ADK は実行がエラーで終わったときは `after_run_callback` を呼ばないので、Stop も起動しない（全実行の記録には使えない）。Stop は最後のイベントの後、応答のストリームを閉じる前に動くので、遅い hook はその間ストリームを開いたままにする（`timeout` を短くする）
- 検証: `agent/tests/test_hooks.py`（読み込み・exit 2・JSON の拒否・書き換え・タイムアウト・Claude Code 用スクリプトの互換）、`agent/tests/test_harness.py::TestContextHarnessPluginHooks`（結線、Stop がブロックしないこと）

## 4. 残りのギャップとバックログ（優先度順）

各項目は GitHub Issue 化して [DAK Project #7](https://github.com/users/teeppp/projects/7) で管理している。

> 2026-09 追記: Codex CLI / OpenCode / Gemini CLI / Goose / Claude Code ほかの OSS ハーネスを実装レベルで
> 調査し、追加のバックログ（#99〜#118、Epic #119）を起票した。調査本文と横断比較は
> `docs/comparison/harness-survey-2026-09/README.md`。下表の #85〜#94 にも設計参照をコメントで追記済み。

| 優先 | 項目 | 狙い | 関連 |
|---|---|---|---|
| ~~P1~~ | ~~**調査用サブエージェント（`AgentTool`）**~~ | **済み（#85）**: `dak_explorer`。§3 の「調査サブエージェント」。隔離環境で親と同じ作業場所を読むのは #527、承認は #535 | #85, #527, #535 |
| P1 | **内容検索ツール（grep）と行番号付き読み込み** | 今の `search_files` はファイル名しか検索できず、中身を探すにはファイル全体を読むしかない。`grep(pattern, path, glob)` と `edit_file`（文字列置換）を足すか、ADK `EnvironmentToolset` への移行を検討 | #86, #16, #20 |
| ~~P1~~ | ~~**TODO ツール（セッション state に保存）**~~ | **済み（#87）**: `write_todos` / `read_plan`。上の「計画と進捗」 | #87, #21 |
| ~~P2~~ | ~~**コンテキスト超過からの回復**~~ | **済み（#88）**: 圧縮側は §5、モデル呼び出し側は §3 の 4（予算を絞って有限回呼び直し、尽きたら説明文で終える） | #88 |
| P2 | **窓サイズの自動検出** | llama-server の `/props`（`n_ctx`）から窓を取る。compose 既定の 8192 と実サーバーの 32768 のようなずれを防ぐ | #89 |
| P2 | **`SkillToolset` への移行** | 独自の `SkillRegistry`/`enable_skill` を ADK 標準（Agent Skills 仕様・段階的開示・リソース読み込み）に寄せ、保守コストを下げる | #90, #81 |
| ~~P2~~ | ~~**ツール失敗の自己修正**~~ | **済み（#91）**: 比べて不採用。理由と上限・停止条件は `docs/design/reflect_retry_plugin.md` | #91 |
| P2 | **モード切替の整理** | 圧縮とスキルで役割の多くが代替されたので、Meta-LLM によるモード切替を残すかどうかを評価で判断する（動的ツール削減 #81 と合わせて検討） | #92, #81 |
| P3 | **長時間タスクの評価** | nightly-eval に「リポジトリ調査」系の長いゴールデンシナリオを加え、窓超過率・圧縮回数・トークン数を Langfuse の指標で追う | #93, #5, #71 |
| P3 | **プロンプトキャッシュ** | `ContextCacheConfig`（Gemini/Anthropic）でコストと遅延を下げる | #94 |
| P3 | **サンドボックス** | ファイル・コマンド系ツールの分離 | #20, #31, #43, #80 |

P3 の「プロンプトキャッシュ」について決めたこと（既定で有効にするかは `docs/design/prompt_cache_evaluation.md`、#94。先頭の安定は #105）:

- **圧縮の要約リクエストにはキャッシュの書き込みを求めない**（#266）: 要約器（`BudgetedEventSummarizer`）が送るのは毎回内容が変わる一回限りのプロンプトで、後のリクエストと先頭を共有しない。書き込んでも再利用されず、書き込みの割増しだけが乗る。今の要約器は自分で `LlmRequest` を組むので、App がキャッシュを有効にしても `cache_config` は `None` のままで、キャッシュの印（`cache_control` など）は付かない（`agent/tests/test_context_cache_interaction.py` の `test_cache_config_survives_compaction` が固定）。印に依らずプロバイダが自動でキャッシュするもの（OpenAI の自動キャッシュ、Gemini の暗黙のキャッシュ。`docs/design/prompt_cache_evaluation.md` の 5 章）はこの方針の外で、DAK からは抑えていない。プロバイダ側で明示的に抑える必要が出たら #94 で扱う
- **モード切替の前後で、キャッシュが効くために変えたくない先頭と、変わってよい末尾**（#264）: プレフィックスキャッシュは先頭から一致する所までしか効かない。変えたくないのは先頭に来る **system 指示とツール定義**（OpenAI 形式の本文では messages[0] の system と `tools`）と、**履歴の古い部分**。変わってよいのは**末尾**（最新のユーザー発話、ツールの結果、差分で注入する情報）だけ。今の `switch_mode` はこの境界を守らない: Meta-Agent の出力で system 指示を丸ごと作り直し、ツール集合も選び直す（列挙してマスクする方式ではない）。`tests/integration/test_mode_cache_prefix.py` の台本（ファイルを読むモードへの切替）では、system 指示の共通の先頭は 0 文字で、ツールは `read_file` が加わった。テストはツール集合が変わることを固定しており、変わらなくなったら落ちる。Meta-Agent が前と同じ指示とツールを選べば先頭は変わらないが、指示は毎回モデルが生成するので一致は当てにできない。計画の書き換えも system 指示を変える（`docs/design/prompt_cache_evaluation.md` の 3 章）。先頭を安定させるか、モード切替を残すかは #92 で決める
- **nightly ではキャッシュのヒット率を記録しない**（#265、2026-10-07 利用者の判断）: nightly-eval（`.github/workflows/nightly-eval.yml`）は Ollama を LiteLLM の `ollama_chat` 経路で呼ぶ。litellm 1.102.0 はこの経路で usage の `prompt_tokens` に Ollama の `prompt_eval_count` を入れるだけで、キャッシュから読んだトークン数（`prompt_tokens_details.cached_tokens`）を埋めない（`litellm/llms/ollama/chat/transformation.py` の usage の組み立て）。ADK はその欄を `usage_metadata.cached_content_token_count` に写す（`google/adk/models/lite_llm.py` の `_extract_cached_prompt_tokens`）ので、この経路では常に空になる。「プロンプト全体のトークン数 − `prompt_eval_count`」を再利用量とみなす代わりの値も採らない。`prompt_eval_count` が Ollama の KV の再利用分を除いた数かは確かめておらず、Ollama が返さないときは LiteLLM が自前で数えた値で埋めるので、キャッシュが効いても見えなくなる。llama.cpp の `/metrics` は nightly が使う Ollama には無く、llama-server の構成（`docker-compose.llamacpp.yml`）も nightly では使っていない。モード切替が先頭を変えることは上の #264 のテストで確かめる。ヒット率を測るなら、キャッシュの量を返す経路（llama-server の `/metrics`、または実プロバイダの usage。#225）で別に起票する

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
