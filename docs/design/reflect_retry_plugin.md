# ツール失敗の再試行: ReflectAndRetryToolPlugin を採るか

PBI #91（Task #218, #219）。ADK 標準の `ReflectAndRetryToolPlugin`（google-adk 2.8、
`google.adk.plugins.reflect_retry_tool_plugin`）を DAK に入れるかを、今の DAK のツール失敗の扱いと
同じ条件で比べて決める。狙いは、小型モデルの自己修正が止まらなくなるのを防ぐこと。

## 今の DAK のツール失敗の扱い

- `AdaptiveAgent._on_tool_error`（`agent/dak_agent/adaptive_agent.py`）: ツールの例外を
  `{"error": "Tool '<name>' failed: <message>"}` の Observation にしてモデルに返す。AP2 の
  `PaymentRequiredError` だけは支払いの Observation にする（払うかはモデルが決める）。回数は数えない
- `ContextHarnessPlugin.before_tool_callback`（`agent/dak_agent/harness.py`、#170。DAK の既定で有効）:
  同じ呼び出し（ツール名 + 引数）が `DAK_MAX_REPEATED_TOOL_CALLS`（既定 3）回を超えて続いたら、ツールを
  実行せずに `repeated_call` の Observation を返す。invocation のツール呼び出しが `DAK_MAX_TOOL_CALLS`
  （既定 40）回を超えたら `step_limit_exceeded`、`DAK_MAX_WALL_SECONDS`（既定 300）秒を超えたら
  `wall_time_exceeded`。どれも例外は投げない。成功と失敗を区別せずに数える

PBI を起こしたとき（ハーネスの 0 段目の前）は「失敗の繰り返しを止める仕組みが無い」状態だった。今は
ハーネスが有効なら同じ呼び出しの繰り返しは 3 回で止まる。

## ReflectAndRetryToolPlugin がすること（2.8 のソース）

- `on_tool_error_callback`（例外）と `after_tool_callback`（`extract_error_from_result` を上書きしたとき、
  結果の中のエラー）で、ツールごとの連続失敗を数える。成功で 0 に戻る。数える範囲は
  `TrackingScope.INVOCATION`（既定）か `GLOBAL`
- `max_retries`（既定 3）回までは、エラー・引数・「同じ呼び出しを繰り返すな」という反省の指示
  （引数が空で約 1,000 文字）を Observation として返す
- 上限を超えたとき:
  - `throw_exception_if_retry_exceeded=True`（既定）: ツールの例外を投げ直す。ADK のプラグイン管理が
    `RuntimeError` に包み、`Runner.run_async` から出る。**invocation は答えを返さずに例外で終わる**
  - `False`: 「このツールをもう使うな」という Observation を返すだけ。モデルが無視すれば呼び出しは続く
    （呼び出しを止める仕組みは無い）
- ADK はプラグインの `on_tool_error_callback` をエージェントの callback より先に呼び、プラグインが答えたら
  エージェントの callback を呼ばない（`google/adk/flows/llm_flows/functions.py` の
  `_run_on_tool_error_callbacks`）。このプラグインは例外に必ず答える（か投げる）ので、
  **`AdaptiveAgent._on_tool_error` は呼ばれなくなる**

## 比較（#218、`agent/tests/test_reflect_retry.py`）

条件: DAK の `AdaptiveAgent` に、スクリプトのモデル（同じツールを同じ引数で呼び続け、成功したか 20 回
呼んだら答える。指示を無視する最悪の小型モデル）とツール 1 つ。シナリオは「常に失敗」と「3 回失敗して
4 回目で成功」。トークンはモデルに送ったリクエストの見積もり（`harness` の見積もり関数）の合計。
Docker・実 LLM は使っていない（2026-09-30、google-adk 2.8）。

| シナリオ | 組 | ツール実行 | モデル呼び出し | 送ったトークン | 成功 | 例外で終了 |
|---|---|---|---|---|---|---|
| 常に失敗 | プラグイン無し（`_on_tool_error` のみ） | 20 | 21 | 5,943 | — | いいえ |
| 常に失敗 | `ContextHarnessPlugin`（DAK 既定） | 3 | 21 | 9,615 | — | いいえ |
| 常に失敗 | ReflectAndRetry（ADK 既定、`max_retries=3`） | 4 | 4 | 2,574 | — | **はい** |
| 常に失敗 | ReflectAndRetry（throw 無し） | 20 | 21 | 45,363 | — | いいえ |
| 一過性 | プラグイン無し | 4 | 5 | 1,009 | はい | いいえ |
| 一過性 | `ContextHarnessPlugin` | 3 | 21 | 9,615 | **いいえ** | いいえ |
| 一過性 | ReflectAndRetry（ADK 既定） | 4 | 5 | 3,682 | はい | いいえ |
| 一過性 | ReflectAndRetry（throw 無し） | 4 | 5 | 3,682 | はい | いいえ |

読み方:

- ReflectAndRetry が安く止まるのは、例外で invocation ごと終わらせる既定のときだけ。そのとき利用者には
  答えが返らない（A2A・CLI の呼び出し元にはエラーになる）
- throw 無しでは、指示を無視するモデルに対して呼び出し回数は減らず、反省の指示が毎回付くぶん
  トークンはプラグイン無しの約 7.6 倍になる。成功する場合でも約 3.6 倍
- 成功率（一過性）はプラグイン無しと同じ。この条件では、反省の指示が成功を増やすことは測れない
  （スクリプトのモデルは指示を読まない。下の「未検証」）
- AP2: プラグインを入れると `PaymentRequiredError` が支払いの Observation にならず、反省の指示になる
  （`test_plugin_pre_empts_ap2_payment_observation`）。支払いの判断をモデルに渡す流れが壊れる
- ハーネスのガードは実行を 3 回で止めるが、止めた後もモデルが呼び続ければモデル呼び出しは続く（上限は
  `DAK_MAX_TOOL_CALLS`・`max_llm_calls`・`DAK_MAX_WALL_SECONDS`）。また、同じ引数で 4 回目に成功するはずの
  呼び出しも止める

## 判断

**採用しない**（2026-09-30）。理由:

1. 止め方が DAK の原則と合わない。上限を確実に守るのは例外で invocation を終える既定だけで、
   ハーネスの「例外にせず、理由付きの Observation を返してモデルに決めさせる」
   （`docs/architecture/harness_engineering.md` §3 の 0）と逆になる。throw 無しでは回数の上限にならない
2. AP2 の `PaymentRequiredError` を `AdaptiveAgent._on_tool_error` より先に握りつぶす。入れるなら
   サブクラスで支払いのエラーを素通しさせる改修が要り、標準のプラグインを使う利点が薄れる
3. 無限の再試行を止める役目は、ハーネスの 0 段目（#170、PBI #99）が既に担っている。上限と停止条件は
   そちら: 同じ呼び出しは `DAK_MAX_REPEATED_TOOL_CALLS`（3）回まで、invocation の呼び出しは
   `DAK_MAX_TOOL_CALLS`（40）回まで、`DAK_MAX_WALL_SECONDS`（300）秒まで、モデル呼び出しは
   `dak:max_llm_calls` / `RunConfig.max_llm_calls`。どれも超えたら理由付きの Observation を返す
4. 反省の指示は失敗 1 回ごとに約 1,000 文字（に引数とエラー）を足す。小さい窓（8K）のモデルでは、それ自体が圧縮を早める

## 未検証のこと

- 実際の小型モデル（Ollama の Qwen など）が反省の指示に従って別の手を取るか、それで成功率が上がるか。
  スクリプトのモデルは指示を読まないので、この比較は「指示を無視するモデルでの上限の効き方」と
  「指示のぶんのコンテキスト消費」だけを測っている。実 LLM での比較は nightly-eval（`docs/eval/`）に
  失敗するツールのシナリオを足せば測れる（このPBIの必須条件ではない）
- ハーネスの繰り返しガードが、一過性の失敗の正当な再試行（同じ引数での 4 回目）を止めること。
  既定の 3 で困る実例はまだ無い。困ったら `DAK_MAX_REPEATED_TOOL_CALLS` を上げるか、失敗の Observation に
  「同じ引数で再試行してよいか」を書く案を検討する
- `GLOBAL` スコープ（セッションをまたいで数える）の振る舞い。INVOCATION だけを測った

## 再検討するとき

実 LLM の評価で反省の指示が成功率を上げると分かったら、プラグインではなく、`_on_tool_error` が返す
Observation に短い指示（何回目か、同じ引数を避けること）を足す形を先に試す。例外で終えず、AP2 の
分岐も保てる。ReflectAndRetry を使うなら `throw_exception_if_retry_exceeded=False`・
`TrackingScope.INVOCATION`（ハーネスのカウンタと同じく invocation ごと）で、`PaymentRequiredError` を
素通しさせるサブクラスにし、`App(plugins=[...])` では `ContextHarnessPlugin` の後に置く（繰り返しの
ガードが先に止めた呼び出しは実行されず、プラグインはその結果を成功とみなして数え直す）。
