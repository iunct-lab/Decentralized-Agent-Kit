# 権限境界: agent と MCP サーバのどこで許可・確認・拒否を強制するか

PBI #16。この文書は、ツール実行の権限（許可 / 確認 / 拒否）を**今どこで、どこまで強制できるか**を、
ローカル実行とリモート実行に分けて比べ、その比較をもとに境界をどこに引くかを決める（「決定」節。#166）。

用語:

- **ローカル実行**: 同じ Docker Compose の中の `mcp-server` にツールを実行させる構成（`docker-compose.yml` の既定）
- **リモート実行**: agent が別ホストの MCP サーバに接続する構成（`MCP_SERVER_URL` を差し替える、`agent_config.yaml` の `mcp_servers`、呼び出し元が `dak:tools` で渡す MCP サーバ）
- **Workspace MCP**: 作業ディレクトリ（ファイルとコマンド）を MCP のツールとして出すサーバ。2026-09-23 の #16 のコメントにより、ファイル・コマンドは MCP 経由で扱う前提にする（下の「Workspace MCP の置き場」）

## 現状（2026-09-29、main 404b80b）

### agent 側: MCP のツールは確認なしで動く

- `agent/dak_agent/agent.py:37-46` は既定のツールセットを `PatchedMcpToolset(..., require_confirmation=True)` で作り、`AdaptiveAgent` に渡す（`agent.py:53`、`100-109`）
- しかし `AdaptiveAgent.__init__` はツールセットを捨てて組み込みツールだけで始める（`agent/dak_agent/adaptive_agent.py:76-91`）。既定のツールセットは「あった」という印（`_has_default_mcp_toolset`、`adaptive_agent.py:122`）と、モード切替の Meta-Agent に選択肢のツール名を見せる一覧（`adaptive_agent.py:613-627` の `get_tools()`）にしか使われない。**`require_confirmation=True` の付いたツールセットはモデルに渡らない**
- MCP のツールは毎回の呼び出しで組み直すツール一覧（`adaptive_agent.py:428` の `live.tools = ... _resolve_session_tools(...)`）からだけ出る。その中のツールセットは全部 `_cached_mcp_toolset`（`adaptive_agent.py:356-371`）→ `make_mcp_toolset`（`agent/dak_agent/skill_tools.py:110-129`）で作られ、`require_confirmation=False` 固定。使われるのは次のとき
  - スキルの有効化・モード切替（`adaptive_agent.py:289-332`。既定の MCP サーバ `self._mcp_url` にも使う。モード切替でツールが選ばれなければ、既定の MCP サーバを絞らずに全部出す: `adaptive_agent.py:326-329`）
  - 呼び出しごとの `dak:tools`（`adaptive_agent.py:256-287`、`341-354`）
  - したがって**既定の MCP サーバの `run_command` / `write_file` も、今は確認なしで実行される**。`agent.py:45` の `require_confirmation=True` は効いていない
  - 以前の文書のうち `docs/comparison/harness-survey-2026-09/README.md:61` の「`require_confirmation` 一律 + denylist」は、この事実と食い違う（調査時点の見立て。denylist も無い）
  - 確かめ方: `cd agent && uv run python` で `AdaptiveAgent(..., tools=[McpToolset(..., require_confirmation=True)])` を作ると、`agent.tools` にツールセットは 0 個、`_has_default_mcp_toolset` は `True`、`_cached_mcp_toolset(url, "http", {"run_command"})._require_confirmation` は `False`（2026-09-29 に実行）
- スキルのローカルツールも確認なし: `skill_tools.py:93-94` の `FunctionTool(func, require_confirmation=False)`
- ツールと引数を見て許可・拒否する仕組み（`before_tool_callback` など）は無い。`AdaptiveAgent` が付ける callback は `before_agent_callback`・`after_model_callback`・`on_tool_error_callback`（`adaptive_agent.py:87-95`）だけ。App のプラグインにはツールの後の hook がある（`ContextHarnessPlugin.after_tool_callback`、`agent/dak_agent/harness.py:719`、`agent.py:114` で App に付ける）。プラグインの `before_tool_callback` にすれば、同じ App のどのエージェントのツール呼び出しにも効く
- 接続先の制御はある: 呼び出し元が渡す MCP サーバは `DAK_ALLOWED_MCP_URLS` の許可リストで絞る（`agent/dak_agent/call_config.py:192-197`）。リダイレクトは追わない（`skill_tools.py:103-107`、`114-119`）。これは「どこに繋ぐか」の制御で、「どのツールをどの引数で実行するか」の制御ではない

### mcp-server 側: 権限の強制は何も無い

`mcp-server/main.py` 全体（290 行）を読んだ結果:

- パスの閉じ込め（confine）が無い: `read_file`（`main.py:82-105`）・`write_file`（`107-122`）・`list_files`（`124-135`）・`search_files`（`164-180`）・`grep`（`182-226`）・`edit_file`（`228-260`）は、受け取った `path` をそのまま `open` / `os.listdir` / `os.walk` に渡す。起動時に `/projects` へ `chdir` するだけ（`main.py:263-278`）で、絶対パスや `..` で外に出られる
- 任意コマンドを実行できる: `run_command`（`main.py:137-162`）は `subprocess.run(command, shell=True, timeout=60)`
- denylist・保護パス・監査ログは無い。`mcp-server/policy.py` も存在しない（旧 Issue 本文が前提にしていた「既存の policy.py」は事実と異なる）
- あるのは出力の上限（`main.py:37-70`）と DNS rebinding 対策（`Host` / `Origin` の許可リスト、`main.py:12-35`）だけ。後者は接続元の制限で、ツール実行の権限ではない
- 認証は無い。`Host` ヘッダが許可リストに合えば、誰でも全ツールを呼べる
- コンテナは root で動く（`mcp-server/Dockerfile` に `USER` が無い）

### 実行環境（docker-compose.yml）

- `mcp-server` はリポジトリのルートを `.:/projects` で read-write にマウントする（`docker-compose.yml:49-50`）。`.env` や `.git` も書ける
- `mcp-server` は `env_file: .env` を読む（`docker-compose.yml:44-45`）ので、`run_command` から `.env` の値（API キー）を環境変数として読める
- Docker ソケット（`/var/run/docker.sock`）はマウントしていない。ネットワーク・CPU・メモリ・PID の制限も無い
- ホストの `8001` に公開している（`docker-compose.yml:43`）

## 比較表

強制点は 3 種類。**agent 側**は agent プロセスの中でツール呼び出しの前に判断する場所（今は McpToolset の `require_confirmation` だけ。ADK の `before_tool_callback`（エージェントかプラグイン）を足せばツール名と引数で判断できる）。**MCP サーバ側**はツールを実行するプロセスの中（`policy.py` 相当。今は無い）。**実行環境**はそのプロセスの外（コンテナの設定）。

| 強制点 | ローカル実行（同一 Compose の mcp-server）で強制できること | リモート実行（別ホストの MCP サーバ）で強制できること | 迂回可能性 | 実装コスト |
|---|---|---|---|---|
| agent 側: McpToolset の `require_confirmation`（現状） | ツールセット単位の確認だけ。ただし今モデルに渡るツールセットは全部確認なし（`skill_tools.py:125-129`）。確認つきの既定のツールセット（`agent.py:43-46`）は使われない（`adaptive_agent.py:76-91`） | 同じ。接続先がどこでも agent の中で止まる。呼び出し元の MCP サーバは許可リストで接続先を絞れる（`call_config.py:192-197`） | 高い。今は何も止めていない。付け直しても、ツールセットを組み直す箇所（`adaptive_agent.py:356-371`）ごとに設定が要り、1 つ漏れれば外れる。MCP サーバに直接つなげば agent を通らない | 小（`make_mcp_toolset` の引数）。ただしツールセット単位で、ツールや引数では分けられない |
| agent 側: `before_tool_callback`（未実装。#101） | ツール名 × 引数で allow / ask / deny。どのツールセットから来た呼び出しにも同じ規則を当てられる | 同じ。MCP サーバが自分のものでなくても、agent を通る呼び出しには効く | 中。agent を通る呼び出しには効くが、MCP サーバに直接つなぐ相手（`8001` に届く別のクライアント）には効かない。引数の文字列で判断するので `run_command` の中身（`sh -c` の組み立て）は見抜けない | 小〜中。agent 内の callback 1 つと規則の読み込み。セッションの状態は agent が持っている |
| MCP サーバ側: `policy.py` 相当（未実装。#109） | パスを実際に解決した後で閉じ込め・保護パス（`.git` / hooks / workflow）を強制できる。`run_command` の実行ユーザ・作業ディレクトリも決められる | 自分で運用する MCP サーバなら同じことができる。**他者の MCP サーバには何も強制できない** | 低い（そのサーバを通る限り、呼び出し元に関係なく効く）。ただし `run_command` がある限り、シェルからは閉じ込めを抜けられる | 中。サーバごとに実装が要る。規則を agent と二重に持つと食い違う |
| 実行環境: コンテナの隔離（#20） | マウント範囲・実行ユーザ・ネットワーク・CPU / メモリ / PID の上限。今はリポジトリのルート全体を read-write、root、制限なし | 相手のホストの設定次第。agent からは確かめられない | 最も低い（プロセスの外で効く）。`run_command` も抜けられない | 中〜大。セッションごとに作るなら常駐・後片付けが要る |

読み取れること（判断ではなく事実の整理。判断は「決定」節）:

- **許可・確認・拒否の「判断」はどの構成でも agent 側で行える**（agent を通る呼び出しに限る）。リモートの MCP サーバが他者のものでも効くのは agent 側だけ
- **物理的な「強制」は実行する側でしかできない**。パスの閉じ込め・保護パス・`run_command` の封じ込めは MCP サーバ側か実行環境でしか効かない
- 今の agent 側の確認は効いていない（確認つきのツールセットが捨てられ、組み直したものは確認なし）。判断を agent 側に置くなら、ツールセットの設定ではなく呼び出しごとの callback にしないと、ツールセットの出どころ（既定 / スキル / `dak:tools`）で食い違う
- 今の mcp-server は認証も閉じ込めも無く、`8001` に届く相手なら誰でもリポジトリと `.env` を読み書きできる

## Workspace MCP の置き場

2026-09-23 の #16 のコメント: ファイル・コマンドは基本的に MCP 経由でよい。Workspace MCP のようなものを扱える前提にする。それを**このリポジトリの中に置くか、別に作るか**が要設計判断。

今の `mcp-server/` は、実質的にこのリポジトリの中の Workspace MCP（ファイル読み書き・検索・コマンド実行、`main.py:73-260`）。選択肢を比較だけしておく:

| 置き場 | 権限の強制 | 疎結合（CLAUDE.md の規約） | 維持の負担 |
|---|---|---|---|
| A: このリポジトリの `mcp-server/` を Workspace MCP として育てる | `policy.py` 相当（閉じ込め・保護パス）と実行環境の隔離を、DAK のテスト（`tests/integration/`）で一緒に検証できる | 独立コンテナのまま。agent は MCP の口だけに依存する | ファイル・コマンドのツールを DAK で持ち続ける |
| B: Workspace MCP を別に作る（別リポジトリ・既存の実装を使う） | 強制は Workspace MCP 側の責任。DAK は agent 側の判断（callback）と接続先の許可リストだけを持つ。他者の実装なら閉じ込めの有無を DAK から確かめられない | agent は MCP の口だけに依存する（今と同じ）。DAK の `mcp-server/` は縮小か削除 | DAK は薄くなるが、隔離（#20）の検証先がリポジトリの外に出る |

**A を採る**（2026-09-29、#166 の `## 回答`）。どちらでも、agent 側の判断は MCP サーバの実装に依存しない形（ツール名 × 引数の callback）で置ける。

## 決定

2026-09-29（#166）。上の比較表と、Workspace MCP の置き場 A をもとに決める。

1. **許可・確認・拒否（allow / ask / deny）の判断は agent 側の `before_tool_callback` に一元化する**
   - 根拠: 比較表の 2 行目。どのツールセット（既定 / スキル / `dak:tools`）から来た呼び出しにも同じ規則を当てられ、リモートの MCP サーバが他者のものでも効くのは agent 側だけ
   - ツールセットの `require_confirmation`（比較表の 1 行目）には判断を置かない。ツールセットを組み直す箇所ごとに設定が要り、今まさに 1 か所（`skill_tools.py:125-129`）の `False` で全部が確認なしになっている
   - 置き場は App のプラグインの `before_tool_callback`。ADK 2.8.0 では、プラグインの `before_tool_callback` がエージェントの callback とツールの実行（`McpTool.run_async` の中の `require_confirmation` の判定を含む）より先に呼ばれ、値を返せばツールは実行されない（`google/adk/flows/llm_flows/functions.py:611-632`）。ask は callback の中で `tool_context.request_confirmation(...)` を呼んで返せばよく、`McpTool` が確認に使う仕組みと同じもの（`google/adk/tools/mcp_tool/mcp_tool.py:395-412`、`google/adk/agents/context.py:856`）
2. **mcp-server 側（このリポジトリの `mcp-server/`。`policy.py` は未実装）には、そこでしか物理的に強制できないものだけを置く**
   - 保護パス（`.git`・`.github/workflows`・スキル定義）への書き込みの拒否（#109）。パスを実際に解決した後でしか確かめられないため（比較表の 3 行目）
   - allow / ask / deny の規則の評価は置かない。MCP のツール呼び出しは同期で、途中で人に確認を挟めない（#109 の決定ログ）
3. **実行環境（コンテナ）の隔離は、1・2 とは独立に mcp-server の実行環境として持つ**（比較表の 4 行目。#20）
4. この境界は **agent を通る呼び出しにしか効かない**。mcp-server に直接つなげる相手（今はホストの `8001`、認証なし）には 1 が効かず、2・3 だけが残る。agent 以外から mcp-server を呼べないようにする（公開をやめる・認証を付ける）ことは、この決定の前提として残す（未検証事項）

リモート実行（別ホストの MCP サーバ）では、自分で運用する mcp-server なら 1〜3 が全部効く。他者の MCP サーバには 1 と接続先の許可リスト（`DAK_ALLOWED_MCP_URLS`、`call_config.py:192-197`）しか効かない。

## #101 への制約

#101（ツールごとの許可・確認・拒否）は、上の決定 1 を実装する PBI。守ること:

- 宣言的な規則の評価、確認要否の判断、deny の Observation 化は、全部 agent 側に置く。mcp-server 側に規則の評価を二重に実装しない
- 判断は 1 つの `before_tool_callback`（App のプラグイン）で行う。ツールセットごとに `require_confirmation` のコーラブルを配る形（#177 の手順が前提にしている形）にはしない。配る箇所を 1 つ漏らせば外れる（`adaptive_agent.py:356-371` と `skill_tools.py:110-129` は今そうなっている）
- プラグインは harness の有無に関係なく App に付ける。今の App のプラグインは `use_harness` のときだけ付く（`agent.py:111-116`）
- deny はツールを実行せず、理由つきの Observation（callback の戻り値）で返す（AGENTS.md の Observation-Driven）
- 呼び出し元が `dak:tools` で渡すツールも同じ callback を通る。確認なしで動かすなら（#101 の決定ログ、#136）、callback の規則で allow にする。ツールセットの設定で迂回しない

## #20 への制約

#20（セッションごとの隔離）は、上の決定 3 を担う PBI。守ること:

- ネットワークの遮断、CPU / メモリ / PID の上限、使い捨ての隔離は、実行環境（mcp-server の側）でしか強制できない。agent 側に判断を一元化したこととは独立に、mcp-server の実行環境の隔離として持つ
- mcp-server 側の隔離は「どの呼び出しを許すか」を判断しない。許された呼び出しを「どの隔離環境で実行するか」だけを受け持つ（#20 の決定ログと同じ）
- Docker ソケット（`/var/run/docker.sock`）を mcp-server に見せるかどうかは、#20 で利用者が判断する（#282）。この文書では決めない
- 置き場 A により、隔離は DAK の統合テスト（`tests/integration/`）で確かめる

## 未検証事項

- 別ホストの MCP サーバに接続した構成は動かしていない。この文書のリモート実行の列はコードから読んだもので、実構成では確かめていない
- リモート構成で、どの利用者のセッションの呼び出しか（と、agent が決めた強制の情報）を mcp-server へ伝える方法は #19 の調査に委ねる
- 決定 1 の callback の順番は ADK 2.8.0 のコードを読んで確かめたもので、動かしてはいない（#101 のテストで確かめる）
- mcp-server を agent 以外から呼べないようにする方法（ホストへの公開をやめる / 認証を付ける）は決めていない。決めるまでは、決定 1 は `8001` に届く相手には効かない
