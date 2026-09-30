# CLI の単発呼び出し契約（stdin・出力形式・exit code）

PBI #17。外部のシステム（シェルスクリプト、CI、別のエージェント）が `dak-cli` を 1 回呼んで、
応答と失敗を機械的に見分けられるようにするための契約を決める。この文書は設計の判断までを扱い、
`cli/src/main.py` の変更は含まない（実装は別の PBI）。

- §1 現状（#256）
- §2 stdin・出力形式・exit code の比較と決定（#256）
- §3 非対話の承認、既存の `run` / `chat` との関係、呼び出しごとの設定の結果の形との整合（#257）

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
| プロンプトの引数が無い | 2 | 何も書かない（Typer（Click）の使い方の誤りは stderr に出る。`CliRunner` は 2 つを混ぜて見せる） |

最後から 2 行目の中身: 承認の問い（`_answer_approvals` の `typer.confirm`、`main.py:69`）は stdin が EOF だと `click.Abort` を投げ、
`run` の `except Exception` がそれを空のメッセージの `Error: ` として飲み込む。`reply_approval` は呼ばれないので、
**agent 側では確認が保留のまま残る**。期限 `DAK_APPROVAL_TIMEOUT_SECONDS`（既定 900 秒）を過ぎても消えず、一覧（`GET /approvals`）に `status: "timed_out"` として出続ける。消えるのは reply が来たときか、同じセッションに次の発言が来たときだけ（`docs/design/approval-queue.md` の 4）。
呼び出し元からは、成功・接続失敗・承認待ちのどれも exit code 0 で、区別できない。

### 同じ CLI の他のコマンド

- `approvals` / `approve`（`main.py:339-404`）は、失敗で `typer.Exit(1)`、オプションの組み合わせの誤りで `typer.Exit(2)` を返す。exit code を使い分けているのはこの 2 つだけ
- `chat`（`main.py:162-290`）は対話ループ。承認は `run` と同じ `_answer_approvals` を使う

## 2. 比較と決定

### 2-a. stdin からのプロンプト・コンテキストの受け取り

| 案 | 呼び方 | 良い点 | 悪い点 |
|---|---|---|---|
| A0. 引数を省いたときだけ stdin を読む（引数があれば stdin は見ない） | `echo "質問" \| dak-cli run` | 引数を渡す呼び出しは stdin を読まないので、開いたままのパイプを受け継いでも止まらない | `cat log \| dak-cli run "要約して"` のように指示とコンテキストを分けて渡せない。呼び出し元が指示と中身を 1 つの文字列に組み立てる。引数とパイプの両方を渡すと、パイプの中身は今と同じく黙って捨てられる |
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
- A0 との差は「引数と、開いたままで何も書かれないパイプの両方がある」ときだけ出る（A は止まり、A0 は止まらない）。A0 はその場合を避ける代わりに、指示とコンテキストを分けて渡す使い方（ログや差分を渡して要約・判定させる。外部システムからの呼び出しの主な形）を失い、パイプの中身を黙って捨てる今の落とし穴も残す。A を採り、止まる場合は呼び出し元が `< /dev/null` を付ける（未検証事項に残す）

### 2-b. 出力形式の切替

| 案 | 呼び方 | 良い点 | 悪い点 |
|---|---|---|---|
| A. `--format markdown\|json`（既定 `markdown`） | `dak-cli run --format json "..."` | 明示的で、ログに残るコマンドから出力の形が分かる。3 つ目の形を足せる | 1 つ長い |
| B. `--json` のフラグ | `dak-cli run --json "..."` | 短い | `gh` の `--json` は「出すフィールドの一覧」を取る別の意味で、同じ名前が別の動きをする |
| C. 環境変数（`DAK_OUTPUT=json`） | `DAK_OUTPUT=json dak-cli run "..."` | 呼び出し側を変えずに切り替えられる | コマンドを見ても出力の形が分からない。親の環境から漏れて、人が読む実行まで JSON になる |
| D. TTY の判定で自動（端末なら Markdown、パイプなら JSON） | `dak-cli run "..." \| jq` | 何も付けなくてよい | 同じコマンドが、ログに流すかどうかで別の形を出す。今パイプで Markdown を読んでいる呼び出し（`cli/README.md` のループの例を `tee` するなど）が黙って変わる |

**決定: A（`--format markdown|json`、既定 `markdown`）。** 既定は今の表示のまま。

`--format json` のとき:

- stdout には **JSON のオブジェクトを 1 つだけ**書く（成功でも、exit 1・3・4 の失敗でも）。待ち表示・承認の表示・警告は stderr に書く
- 例外は使い方の誤り（exit 2）。stdout には何も書かず、理由を stderr に書く。引数・オプションの誤りは Typer（Click）がコマンドの本体より前に判定して stderr に書くので、JSON を組み立てる場所が無い。空の stdin（§2-a）も同じ扱いにそろえる。呼び出し元は exit 2 なら stdout を読まない
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
| A. 0 / 1 だけ | 0 = 成功、1 = それ以外 | 単純。`gh` の成功 0・失敗 1 と同じ（`gh` はほかに取り消し 2・要認証 4 も使う） | 承認待ちと接続失敗を stdout を読まないと区別できない（`--format markdown` では読めない） |
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

## 3. 非対話の承認と既存コマンドとの関係

### 3-1. 現状: 非対話で確認に当たると、保留を残して exit 0

- 確認の要るツールかどうかは agent 側の `PermissionPlugin`（`agent/dak_agent/permission.py`）の規則で決まる。既定の規則（`DEFAULT_RULES`、`permission.py:156-175`）は、既定の MCP サーバのツールを `ask`（読み取りと読み取り専用の `git` だけ `allow`、`rm -rf` と `git push --force` は `deny`）、agent の中のツールと、呼び出し元が `dak:tools` で渡した MCP サーバのツールを `allow` にする。運用者は `agent_config.yaml` の `permissions:` で上書きできる（`load_rules`、`permission.py:265`）
  - Task #257 の本文が前提にしていた「`agent/dak_agent/agent.py:45` で MCP は既定 `require_confirmation=True`」は今のコードと違う。`agent.py` は `require_confirmation` を付けず、コメントで「allow / ask / deny は `PermissionPlugin` が決める」と書いている（`agent.py:44-45`）
- ターンが確認で止まると、`AgentClient._needs_approval`（`cli/src/client.py:18-35`）が ADK のイベントから**最初の 1 件**の確認を `{"status": "needs_approval", "tool_call": {...}}` にする
- `run` と `chat` は同じ `_answer_approvals`（`cli/src/main.py:59-77`）で `typer.confirm` を出し、答えを `POST /approvals/{id}/reply` で送る。答えた後に次の確認が来れば繰り返す
- stdin が端末でない（EOF）と `typer.confirm` が `click.Abort` を投げ、`run` はそれを `Error: ` として飲み込み exit 0 で終わる（§1 の表）。**reply は送られず、agent 側に確認が保留のまま残る**。保留は `DAK_APPROVAL_TIMEOUT_SECONDS`（既定 900 秒）を過ぎると一覧で `timed_out` になり、同じセッションの次の発言で捨てられる（`docs/design/approval-queue.md` の 4）。`run` は毎回新しいセッションを作るので、次の発言は来ない
- 保留は別のクライアントからも答えられる: `dak-cli approvals --session <id>` で一覧を見て、`dak-cli approve <id> --session <id>` で答える（`main.py:339-404`、`GET /approvals` と `POST /approvals/{id}/reply`）。ただし今の `run` は `session_id` を表示しないので、呼び出し元はどのセッションか分からない
- `ask_question`（モデルから利用者への質問）でターンが終わったときも、保留として `GET /approvals` に出る（`agent/dak_agent/approvals.py:69-102`）。今の `run` はこれを確認として扱わず、質問の文をツールの結果として表示して exit 0 で終わる

### 3-2. 非対話で確認に当たったときの既定

| 案 | 動き | 良い点 | 悪い点 | 権限の規則との整合 |
|---|---|---|---|---|
| A1. 答えずに止める。保留を構造化して返し exit 3 | reply を送らない。`--format json` に保留の一覧（id・ツール名・引数・`session_id`）を入れる | 判断を人に残せる。保留はそのまま `dak-cli approve` や BFF から答えられる（承認のキューの設計どおり）。LLM をそれ以上呼ばない | 答えが来なければ期限まで保留が残る（それ以上の害は無い。期限後は `timed_out` と表示されるだけで、エージェントは動かない） | `ask` を `ask` のまま扱う |
| A2. その場で拒否する（`reject`、理由つき）。exit 3 | reply `reject` を送り、エージェントに `denied_by_user` を返してターンを続けさせる | 保留が残らない | 利用者が拒否していないのに「利用者が拒否した」とモデルに伝える。ターンが続くので LLM をもう一度呼び（お金がかかる）、モデルが別の手段を試しうる。そのあとの応答を `run` の結果とするか、exit 3 とするかが曖昧 | `ask` を `deny` と同じに扱う |
| B. `--allow-tools <名前,...>` を渡したときだけ、そのツールの確認に CLI が自動で `once` と答える | 一覧にあるツールの確認に reply `once`、ないものは A1 | 無人でも書き込みのツールを使える | 運用者が `ask` にした判断を、CLI の呼び出し元が 1 つのオプションで越えられる。引数を見ない（`run_command` を許せば、どのコマンドも通る）。モデルへのプロンプトで引数を操れる | 運用者の `ask` を呼び出し元が `allow` に変える |
| C. 常にエラーで止める（exit 1） | A1 と同じく reply を送らず、接続失敗と同じ扱い | 単純 | 接続失敗と区別できない。保留の id を返さなければ、後から答えられない | — |

**決定: A1。** 非対話の `run` は、ターンが確認か質問で止まったら reply を送らずに終わる。

- `--format json` の出力は `{"status": "needs_approval", "session_id": "...", "output": "<止まるまでのモデルのテキスト>", "error": "needs_approval", "approvals": [...]}`、exit 3
- `approvals` の要素は `GET /approvals` の要素をそのまま（`id`・`kind`（`approval` / `question`）・`tool_name`・`tool_args`・`questions` など）。取得は、ターンが確認か質問で止まったときだけ `AgentClient.list_approvals(session_id)` を呼ぶ。止まったことは応答のイベントから判定する: 確認は今の `_needs_approval` と同じく `adk_request_confirmation` の functionCall、質問は最後のイベントが `ask_question` の functionResponse で `error` を持たないこと（agent 側の `list_pending_questions` と同じ条件、`approvals.py:77-79`）。`_needs_approval` の 1 件ではなく一覧を返すのは、1 回のモデルの応答が複数の確認を出すことがあるため
- `--format markdown` では、保留ごとに `dak-cli approve <id> --session <session_id>` で答えられることを stderr に書く
- 非対話かどうかは **stdin が端末かどうか**で決める（`typer.confirm` が読む先）。stdin が端末なら、`--format json` でも今までどおり対話で聞く（問いは stderr に出す）

根拠:

- `docs/CHARTER.md` の「System ENABLES, Agent DECIDES」と「暗黙の副作用を足さない」。A2 は人が決めていない拒否を人の拒否としてモデルに伝え、LLM の追加の呼び出し（費用）を黙って起こす。B は運用者が `ask` にしたツールの確認を、呼び出し元が暗黙に飛ばす経路を増やす。A1 は何も実行せず、何も答えない
- 確認なしで動かしたい呼び出し元には、既に明示的な道が 2 つある: 運用者が `agent_config.yaml` の `permissions:` でそのツールを `allow` にする（引数のパターンで絞れる）、または呼び出し元が自分の MCP サーバを `dak:tools` で渡す（`caller` は `allow`。接続先は運用者の `DAK_ALLOWED_MCP_URLS` で絞る）。どちらも「誰が許したか」が運用者の設定に残る。B はそれを CLI のオプションに分散させる
- 承認のキュー（`docs/design/approval-queue.md`）は、保留を別のクライアントから答える前提で作られている。A1 は、外部システムが exit 3 と保留の id を受け取り、人に回して `dak-cli approve` で答えてもらう、という使い方にそのまま乗る
- 質問（`ask_question`）を同じ扱いにするのは、どちらも「人の入力が無いとターンが進まない」で、`GET /approvals` が両方を 1 つの一覧で返すため

### 3-3. `run` と `chat` との関係

| 案 | 良い点 | 悪い点 |
|---|---|---|
| A. `run` に §2 の入力（stdin）と `--format` を足す | 既に単発呼び出しとして文書化されている（`README.md`・`cli/README.md`・`docs/quickstart.md`）。呼び出し元は今のコマンドのまま、必要なときだけオプションを足す | `run` の今の exit code（常に 0）が変わる（§2-c の移行の影響） |
| B. 非対話専用の新しいコマンド（例: `dak-cli call`）を作る | `run` の挙動を変えない | 単発で 1 ターンを実行するコマンドが 2 つになり、応答の組み立て・承認の扱いが 2 か所に分かれる。どちらを使うべきかを文書で説明し続けることになる |

**決定: A。** 単発呼び出しは `run` に一本化し、`chat` は対話専用のまま（`--format`・stdin・exit code の契約を足さない）。

- `run` と `chat` が共有するのは、応答の組み立て（`_extract_response_text`、`main.py:24-57`）と、端末があるときの承認（`_answer_approvals`）だけ。今の `run` は `_extract_response_text` と同じ処理を自前で持っている（`main.py:115-151`）ので、`--format markdown` の表示は実装の PR でそれを使う形にそろえる。`_extract_response_text` はターンの全部のモデルのテキストをつなぐので、`--format json` の `output` と失敗の判定には使わない（§3-4）
- `chat` の enforcer の自動再試行（`main.py:248-267`）は `run` に持ち込まない。再試行は呼び出しごとの検査（#140、`dak:inspection`）が agent 側で行う

根拠: 1 つの目的に 1 つのコマンド（B は同じことをする 2 つ目の入口を作る）。§2-a で、引数だけの今の呼び方は変わらないことを確かめた。exit code の変化は、失敗を成功と読んでいた呼び出しを正す変化（§2-c）。

### 3-4. 呼び出しごとの設定の結果の形との整合

呼び出しごとの設定（`docs/architecture/call_config.md`）は、失敗したとき**応答テキストを JSON のオブジェクト 1 つに差し替える**。どれも `error` に種別を持つ:

| 種別 | 出す場所 | ほかのキー | 状態（2026-09-30） |
|---|---|---|---|
| `output_schema_validation_failed` | `agent/dak_agent/adaptive_agent.py:579`（#137） | `issues` | main |
| `model_not_allowed` | `agent/dak_agent/call_config.py:140`（#138） | `requested_model`・`allowed_models` | main |
| `invalid_tools` | `call_config.py:210`（#136） | `expected` | main |
| `invalid_mcp_servers` | `call_config.py:241`（#136） | `expected` | main |
| `mcp_server_not_allowed` | `call_config.py:248`（#136） | `requested_urls`・`allowed_urls` | main |
| `turn_limit_exceeded` | #134（Task #202 の予定） | `limit`・`calls_used`・`elapsed_seconds` | 未実装 |
| `inspection_failed` | #140（Task #245 の予定） | `attempts`・`issues`・`last_response` | 未実装 |

`dak:tools_error`（#136）は失敗ではない。呼び出し元の MCP サーバに繋がらなくてもターンは続き、理由はセッションの state（`/run` の応答のイベントの `stateDelta`）に残る。

**決定: `--format json` は、構造化された失敗のオブジェクトをトップレベルにそのまま透過させる。**

```json
{"status": "failed", "session_id": "…", "output": "{\"error\": \"model_not_allowed\", …}",
 "error": "model_not_allowed", "requested_model": "openai/not-allowed", "allowed_models": ["openai/fake-default"]}
```

- 「モデルの最終テキスト」は、そのターンの**最後のモデルのイベントのテキストだけ**（途中でツール呼び出しと一緒に書いたテキストはつながない。`output_schema` の検査もツール呼び出しを含む応答を飛ばす、`adaptive_agent.py:568`）。`output` も同じ文字列にする
- それが JSON のオブジェクトで、`error` が上の表の種別のどれかなら、`status: "failed"`・exit 4。そのオブジェクトのキーをすべてトップレベルに置く（`error` の値は §2-b の `error` と同じになる）。§2-b のキー（`status`・`session_id`・`output`）と名前が重なるときは §2-b のキーを残す（今の種別のキーはどれも重ならない）
- 表に無い種別の `error` を持つ JSON は、モデルの普通の応答として `succeeded`（`dak:output_schema` で呼び出し元が `error` という項目を定義することがあるため）。種別を足したときは、CLI の一覧にも足す
- `dak:tools_error` がそのターンのイベントの `stateDelta` にあれば、`tools_error` として同じ値を入れる。`status` は変えない（失敗ではないため）
- A2A・HTTP の呼び出し元が応答テキストを `json.loads` して `error` を見るのと、CLI の呼び出し元が stdout を `jq .error` で見るのとで、同じ種別名・同じキーで判定できる

根拠:

- 呼び出しごとの設定は HTTP・A2A・CLI のどれから呼んでも同じ agent の同じ処理を通る。CLI だけ別の名前（例: `{"failure": {"kind": ...}}` に包む）にすると、呼び出し元は経路ごとに判定を書き分けることになる。PBI #17 の決定ログ（2026-09-21）の「CLI の単発呼び出し契約は、これらの結果の形と揃える」
- 入れ子にせずトップレベルに置くのは、`call_config.md` の例（`{"error": ..., "issues": ...}`）をそのまま読めるようにするため。§2-b のキーと重ならないことは上の表で確かめた
- 種別を知っているものに限るのは、呼び出し元のスキーマの `error` 項目を失敗と読み違えないため

### 未検証事項（§3）

- モデル自身が、表の種別と同じ `{"error": "model_not_allowed", ...}` を応答として書いたとき（プロンプトで誘導されたときなど）、CLI はそれを構造化された失敗と読む（exit 4）。失敗側に倒れるので害は小さいが、agent が失敗であることを応答の外（イベントの `customMetadata` など）で示せば区別できる。agent 側の変更になるので、この PBI では決めない
- 1 回のモデルの応答が確認と質問を同時に出したときの扱いは、承認のキューでも決まっていない（`docs/design/approval-queue.md` の 3）。A1 は両方を `approvals` に並べて返すだけで、どちらに答えるべきかは示さない
- 複数の確認のうち 1 件に答えると、同じターンの残りの確認は `reject` になる（#406）。呼び出し元が `approvals` の全部に順に答えようとすると、2 件目以降は `404` になる。`dak-cli approve` の出力でそれが分かるかは確かめていない
- `dak-cli approve` 自体には `--format json` も exit code の契約も無い（今は失敗で 1、使い方の誤りで 2 だけ）。非対話の呼び出し元が承認の後の応答を機械的に読むには、`approve` にも §2 の契約が要る。範囲を決めるのは実装の PBI
- 期限（`DAK_APPROVAL_TIMEOUT_SECONDS`）を過ぎた保留は、一覧では `timed_out` と出るが、答えないかぎり消えない。非対話の呼び出しが多いと、答えられない保留を持つセッションがたまる。セッションの後片付けの要否は確かめていない
- `turn_limit_exceeded`（#134）と `inspection_failed`（#140）のキーは、それぞれの Task の本文にある予定の形で、まだ main に無い。実装で変われば、この表と CLI の一覧を合わせて直す
- `run` から `dak:` キー（`state_delta`）を渡す口は無い（§1）。CLI から呼び出しごとの設定を使えるようにするか（`--instruction`・`--output-schema` など）は、この PBI の範囲外
