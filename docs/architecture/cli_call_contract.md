# CLI の単発呼び出し契約（stdin・出力形式・exit code）

PBI #17。外部のシステム（シェルスクリプト、CI、別のエージェント）が `dak-cli` を 1 回呼んで、
応答と失敗を機械的に見分けられるようにするための契約を決める。この文書は設計の判断までを扱い、
`cli/src/main.py` の変更は含まない（実装は別の PBI）。

- §1 現状（#256）
- §2 stdin・出力形式・exit code の比較と決定（#256）

用語:

- **単発呼び出し**: 1 回のプロセス起動で 1 ターンを実行し、結果を出して終わる呼び出し。今の `dak-cli run`
- **非対話**: 端末の前に人がいない実行。stdin が端末（TTY）でない、または答える人がいない
- **構造化された失敗**: 呼び出しごとの設定（`docs/architecture/call_config.md`）が、応答テキストの代わりに返す
  `{"error": <種別>, ...}` の JSON（`output_schema_validation_failed`・`model_not_allowed` など）

## 1. 現状（2026-09-30、main 8241c5b）

`dak-cli run`（`cli/src/main.py:103-158`）と、その下の `AgentClient.run_task`（`cli/src/client.py:100-125`）を読み、
偽の `AgentClient` を `typer.testing.CliRunner` で呼んで確かめた。

### 入力: 引数だけ、stdin は読まない

- `def run(prompt: str)`（`main.py:104`）。プロンプトは位置引数 1 つで、必須。省くと Typer（Click）の使い方の誤りで exit 2
- stdin は読まない。`cat file | dak-cli run "要約して"` の `file` の中身はエージェントに届かない
- 1 回ごとに新しいセッションを作る（`AgentClient()` が `session_{user}_{uuid}` を作り、`_ensure_session` が作成する。`client.py:44-49`、`68-97`）。続きのセッションを指定する口は無い
- 呼び出しごとの設定（`state_delta` の `dak:` キー）を渡す口も無い。`run_task` の `payload` は `new_message` だけ（`client.py:108-113`）
- `run` は `run_task(prompt, permissions={"default": "ask"})` と渡す（`main.py:111`）が、`run_task` は `permissions` を使わない（`client.py:100-125` のどこにも出てこない）。許可・確認・拒否は agent 側の `PermissionPlugin` の規則だけで決まる（`docs/design/permission-boundary.md`）

### 出力: Rich の Markdown 表示だけ、すべて stdout

- 応答は ADK のイベントの一覧から、ツールの結果（`functionResponse` の `text` か `result`）を先に、モデルのテキストを後に改行でつないだ 1 つの文字列（`main.py:115-151`）
- それを `Panel(Markdown(...), title="Agent Response")` で表示する（`main.py:153-154`）。枠線と端末幅での折り返しが入るので、stdout をそのまま JSON として読めない
- 待ち表示（`console.status`）・承認の表示・エラーも同じ `Console()`（stdout）に書く（`main.py:87`）。stderr は使わない
- 呼び出しごとの設定の構造化された失敗も、他の応答と同じく枠の中の Markdown として出る（確かめ方は下の表）

### 失敗: exit code はいつも 0

`run` の本体は `try: ... except Exception as e: console.print(f"[red]Error:[/red] {e}")`（`main.py:108`、`157-158`）で、`typer.Exit` を呼ばない。
既存の単体テスト `cli/tests/test_main.py::test_run_command_error` も exit code 0 を期待している（「Typer commands succeed even with handled exceptions」）。

2026-09-30 に `AgentClient` を偽物にして `CliRunner` で呼んだ結果:

| 状況 | exit code | stdout |
|---|---|---|
| 応答を得た | 0 | 枠つきの Markdown |
| agent に接続できない（`run_task` が `ConnectionError`） | 0 | `Error: down` |
| 応答が構造化された失敗（`{"error": "model_not_allowed", ...}`） | 0 | その JSON が枠つきの Markdown として出る |
| 確認の要るツールに当たり、stdin が空（非対話） | 0 | 承認の枠、`Allow this tool execution? [y/N]: `、`Error: `（空） |
| プロンプトの引数が無い | 2 | Typer の使い方の誤り |

最後から 2 行目の中身: 承認の問い（`_answer_approvals` の `typer.confirm`、`main.py:69`）は stdin が EOF だと `click.Abort` を投げ、
`run` の `except Exception` がそれを空のメッセージの `Error: ` として飲み込む。`reply_approval` は呼ばれないので、
**agent 側では確認が保留のまま残る**（期限 `DAK_APPROVAL_TIMEOUT_SECONDS` まで、または同じセッションの次の発言で捨てられるまで。`docs/design/approval-queue.md` の 4）。
呼び出し元からは、成功・接続失敗・承認待ちのどれも exit code 0 で、区別できない。

### 同じ CLI の他のコマンド

- `approvals` / `approve`（`main.py:339-404`）は、失敗で `typer.Exit(1)`、オプションの組み合わせの誤りで `typer.Exit(2)` を返す。exit code を使い分けているのはこの 2 つだけ
- `chat`（`main.py:162-290`）は対話ループ。承認は `run` と同じ `_answer_approvals` を使う

## 2. 比較と決定

### 2-a. stdin からのプロンプト・コンテキストの受け取り

| 案 | 呼び方 | 良い点 | 悪い点 |
|---|---|---|---|
| A. 引数を省いたら stdin を読む。引数と stdin の両方があれば、引数を指示、stdin をその後ろに置くコンテキストとしてつなぐ | `echo "質問" \| dak-cli run` / `cat log \| dak-cli run "要約して"` | 引数だけの今の呼び方がそのまま動く。パイプの中身を指示と分けて渡せる | stdin が TTY でないのに閉じられない環境（パイプを開けたまま子プロセスを起動する実行器）では、読み終わりを待って止まる |
| B. `--stdin` を付けたときだけ stdin を読む | `cat log \| dak-cli run --stdin "要約して"` | 読まない限り止まらない。挙動が明示的 | 付け忘れると中身が黙って捨てられる（今と同じ落とし穴が残る） |
| C. 常に stdin を先に見る（TTY でなければ必ず読む。引数より優先） | `cat log \| dak-cli run` | 呼び方が 1 通り | 引数とパイプの両方を渡したときの意味が不明瞭。A と同じく止まりうる |

**決定: A。** 次の 4 通りに決める。

| 引数 | stdin | プロンプト |
|---|---|---|
| あり | TTY | 引数だけ（今と同じ） |
| あり | TTY でない | 引数 + 空行 + stdin の全文。stdin が空なら引数だけ |
| なし | TTY でない | stdin の全文。空なら使い方の誤り（exit 2） |
| なし | TTY | 使い方の誤り（exit 2）。キーボードからの入力を待たない |

根拠:

- 今の呼び方（引数 1 つ。`README.md`・`cli/README.md`・`docs/quickstart.md`・統合テスト `tests/integration/test_cli.py`）を変えずに足せる。B・C は新しい呼び方を覚えさせる
- 「パイプで渡したものは読まれる」は `jq` など、stdin を入力とする CLI の慣習に合う。B は付け忘れで今と同じ「黙って捨てる」が残る
- 読むのは stdin が TTY でないときだけなので、端末から引数付きで呼ぶ人は止まらない

### 2-b. 出力形式の切替

| 案 | 呼び方 | 良い点 | 悪い点 |
|---|---|---|---|
| A. `--format markdown\|json`（既定 `markdown`） | `dak-cli run --format json "..."` | 明示的で、ログに残るコマンドから出力の形が分かる。3 つ目の形を足せる | 1 つ長い |
| B. `--json` のフラグ | `dak-cli run --json "..."` | 短い | `gh` の `--json` は「出すフィールドの一覧」を取る別の意味で、同じ名前が別の動きをする |
| C. 環境変数（`DAK_OUTPUT=json`） | `DAK_OUTPUT=json dak-cli run "..."` | 呼び出し側を変えずに切り替えられる | コマンドを見ても出力の形が分からない。親の環境から漏れて、人が読む実行まで JSON になる |
| D. TTY の判定で自動（端末なら Markdown、パイプなら JSON） | `dak-cli run "..." \| jq` | 何も付けなくてよい | 同じコマンドが、ログに流すかどうかで別の形を出す。今パイプで Markdown を読んでいる呼び出し（`cli/README.md` のループの例を `tee` するなど）が黙って変わる |

**決定: A（`--format markdown|json`、既定 `markdown`）。** 既定は今の表示のまま。

`--format json` のとき:

- stdout には **JSON のオブジェクトを 1 つだけ**書く（成功でも失敗でも）。待ち表示・承認の表示・警告は stderr に書く
- 形（キー名は `call_config.md` の構造化された失敗と揃える。失敗のときの中身の透過は §3 で決める）:

```json
{"status": "succeeded", "session_id": "session_u1_…", "output": "<モデルの最終テキスト>", "error": null}
```

| キー | 値 |
|---|---|
| `status` | `succeeded`（ターンが応答で終わった）/ `needs_approval`（承認待ちで進めなかった）/ `failed`（構造化された失敗）/ `error`（CLI 側の失敗: 接続・HTTP・未ログイン） |
| `session_id` | そのターンのセッション。承認に後から答える（`dak-cli approve --session`）・履歴を読むのに使う。CLI 側の失敗でセッションを作る前に止まったら `null` |
| `output` | モデルの最終テキスト（ツールの結果はつながない）。`dak:output_schema` を使ったときはそのまま `json.loads` できる文字列 |
| `error` | `status` が `succeeded` なら `null`。それ以外は種別の文字列。構造化された失敗なら `call_config.md` と同じ種別名（`model_not_allowed` など）、CLI 側の失敗なら `connection_failed` / `http_error` / `not_logged_in` |

根拠:

- 出力の形を決めるのは呼び出し元なので、呼び出し元のコマンドに見える A が確かめやすい（C・D は、同じコマンドが実行する場所で違う形を出す）
- `--format` の値で選ぶ形は、`claude -p --output-format text|json` など、LLM の CLI の単発呼び出しでも使われている。B の名前の衝突を避けられる
- `output` にツールの結果をつながないのは、`markdown` の表示（ツールの結果を先に出す、`main.py:145-151`）のままだと、`dak:output_schema` の JSON の前に別の文字列が付いて読めなくなるため。ツールの結果が要る呼び出し元は、`session_id` でセッションの履歴を読める

### 2-c. exit code 契約

| 案 | 割り当て | 良い点 | 悪い点 |
|---|---|---|---|
| A. 0 / 1 だけ | 0 = 成功、1 = それ以外 | 単純。`gh` の既定と同じ | 承認待ちと接続失敗を stdout を読まないと区別できない（`--format markdown` では読めない） |
| B. 0 / 1 / 2 / 3（Task の例） | 0 = 成功、1 = 一般エラー、2 = 承認待ちで拒否、3 = 上限・検査 | 成功・拒否・エラーを分けられる | 2 は Typer（Click）の使い方の誤りの exit code（`click.UsageError.exit_code == 2`）と重なる。`approve` も既に 2 を使い方の誤りに使っている（`main.py:381`、`384`） |
| C. 0 / 1 / 2 / 3 / 4 | 0 = 成功、1 = エラー、2 = 使い方の誤り、3 = 承認待ち（拒否）、4 = 構造化された失敗 | B の区別を保ちつつ、2 を Click・`jq` と同じ意味に残す | 覚える数が 1 つ増える |

**決定: C。**

| exit code | 意味 | 呼び出し元がすること |
|---|---|---|
| 0 | ターンが応答で終わった | `output` を使う |
| 1 | CLI 側の失敗（agent に接続できない、HTTP のエラー、未ログイン、想定外の例外） | 再試行か、設定を直す |
| 2 | 使い方の誤り（引数・オプション・空の stdin） | 呼び方を直す。agent には何も送っていない |
| 3 | 確認の要るツールに当たり、非対話のため進めなかった（拒否） | 承認に答える、または規則を変える（扱いは §3） |
| 4 | agent が構造化された失敗を返した（呼び出しごとの設定による拒否・スキーマ不一致・上限・検査） | `error` の種別を見て、呼び出しの指定を直す |

- exit code は `--format` に依らない（`markdown` でも同じ値を返す）。人が端末で使うときも、`&&` や `set -e` で止まる
- 3 と 4 は「agent には届いたが、応答として使えない」。1 は「agent の結果が無い」。呼び出し元の直し方が違うので分ける

根拠:

- 2 を使い方の誤りに残すのは、Typer（Click）が引数の誤りで既に 2 を返し（§1 の表の最後の行）、`jq` も使い方の誤りを 2 にしているため。承認待ちを 2 にすると、`dak-cli run` の typo と承認待ちを区別できない
- 1 を「失敗」にするのは `gh` の慣習（`gh help exit-codes`: 失敗は 1）と同じ
- 承認待ちと構造化された失敗を分けるのは、PBI の「成功・拒否・エラー」の 3 つを exit code だけで見分けるため。拒否（3）は人の判断を待つもの、構造化された失敗（4）は呼び出しの指定の問題で、再試行の要否が違う

#### 「常に 0」からの移行の影響

- 引数だけで呼んで成功する呼び出しは変わらない（exit 0、既定の `markdown` の表示）。`tests/integration/test_cli.py` はこの形
- 失敗していたのに 0 だった呼び出しが 1 / 3 / 4 になる。`set -e` のスクリプトはそこで止まるようになる（`cli/README.md` のループの例は `set -e` を使っていないので止まらない）
- `cli/tests/test_main.py::test_run_command_error` の「exit code 0」の期待は、実装の PR で 1 に変える
- 移行のためのフラグ（旧来の「常に 0」を選べるもの）は作らない。0 を頼りにしていた呼び出し元は、失敗を成功と読んでいたことになるため

### 未検証事項（§2）

- 案 A の stdin の読み取りが、stdin を開けたまま閉じない実行器で止まるか。止まる環境があるなら、呼び出し元に `< /dev/null` を付けてもらうか、読み取りに時間の上限を付けるかを実装の PR で決める
- stdin の大きさの上限。今の `/run` は本文の大きさを制限していない。大きな stdin がどこで失敗するか（CLI・agent・LLM のコンテキスト）は確かめていない
- stdin の文字コード。UTF-8 以外のバイト列を渡したときの扱いは確かめていない
- `console.status`（Rich の待ち表示）を stderr に移したとき、stdout が端末でない場合に Rich が何も書かないか
- `--format json` の CLI 側の失敗の種別（`connection_failed` / `http_error` / `not_logged_in`）で足りるか。今の `run_task` は接続の失敗も HTTP のエラーも `ConnectionError` にまとめている（`client.py:124-125`）ので、分けるには `client.py` の変更が要る
- 既存の `chat` の対話ループとの共存。`chat` は対話専用のままにし、`--format` も stdin の読み取りも足さない想定だが、`chat` の中で同じ出力の組み立て（`_extract_response_text`）を共有するかは実装の PR で決める
- 1 回ごとに新しいセッションを作る今の挙動のままでよいか（続きのセッションを指定する口が要るか）。承認に後から答えるには `session_id` を知る必要があり、`--format json` はそれを返すが、`markdown` の表示は返さない
