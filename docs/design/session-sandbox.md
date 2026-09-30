# セッションごとの使い捨て隔離（SandboxManager）

PBI #20 / Task #282。この文書は、mcp-server のツール（ファイル操作と `run_command`）を**利用者のセッションごとに隔離した環境で実行する**方式を決める。
Docker ソケットを mcp-server に見せる案と、Docker を使わない in-process の案を比べ、既定値と後続の Task（#283 / #284 / #285）が守る制約を書く。
Docker ソケットを `docker-compose.yml` でマウントするかどうかは利用者が決める（`permission-boundary.md` の「#20 への制約」）。この文書では決めず、#20 の `## 判断待ち` で聞く。

調べた版: mcp（mcp-server の依存）1.30.0。パスは `mcp-server/.venv/lib/python3.10/site-packages/` からの相対（以下 `<mcp>` = `mcp`）。

## 前提（#16 の決定から）

- 許可・確認・拒否（allow / ask / deny）の判断は agent 側の `before_tool_callback` に一元化されている（`permission-boundary.md` の「決定」1）。SandboxManager は**どの呼び出しを許すかを判断しない**。許された呼び出しを**どの隔離環境で実行するか**だけを受け持つ（同「#20 への制約」）
- ネットワークの遮断、CPU / メモリ / PID の上限は、実行する側（mcp-server の実行環境）でしか強制できない（同「決定」3）

## 現状（2026-09-30、main cef5bd5）

- mcp-server はリポジトリのルートを `.:/projects` で read-write にマウントし（`docker-compose.yml` の `mcp-server.volumes`）、起動時に `/projects` へ `chdir` する（`mcp-server/main.py` の `lifespan`）。ファイル系ツールは相対パスをこの作業ディレクトリで解決する。全セッションが同じ `/projects` を共有する
- `run_command` は mcp-server のコンテナの中で `subprocess.run(command, shell=True, timeout=60)` する。ネットワーク・CPU・メモリ・PID の制限は無い
- Docker ソケット（`/var/run/docker.sock`）は mcp-server に見せていない（`docker-compose.yml` にも `docker-compose.test.yml` にも無い）。mcp-server のイメージに `docker` CLI も無い（`mcp-server/Dockerfile`）
- 既存の統合テストは共有の `/projects` を前提にしている（`tests/integration/test_mcp_server.py` の `test_read_file_round_trip` はリポジトリの `README.md` を読む）
- セッションの識別: agent は MCP の呼び出しに `X-DAK-Session-Key: <user_id>:<session_id>`（各部分は percent-encode）を付ける（#19、`agent/dak_agent/mcp_headers.py`）。streamable HTTP では `tools/call` の POST ごとに Starlette の `Request` が `RequestContext.request` に入り（`<mcp>/server/streamable_http.py` の `ServerMessageMetadata(request_context=request)` → `<mcp>/server/lowlevel/server.py` の `RequestContext(..., request=request_data)`）、ツール関数は `ctx: Context | None = None` の引数で受け取れる（`<mcp>/server/fastmcp/utilities/context_injection.py` は `Optional[Context]` も注入先として認める）。実際に取れるかは #284 の手順 1 で実機で確かめる
- このヘッダは認証ではない。mcp-server に直接つなぐ相手は任意の値を送れる（`permission-boundary.md` の「決定」4 と同じ想定: `8001` に直接届くのは開発者自身）

## 選択肢

### Docker ソケット案（`SANDBOX_MODE=docker`）

mcp-server が `docker` CLI でセッションごとに使い捨てのコンテナを起動し、ツールをその中で実行する。

- `docker` CLI は `mcp-server/Dockerfile` に `COPY --from=docker:27-cli /usr/local/bin/docker /usr/local/bin/docker` で足す（client のバイナリだけ。daemon は含めない）。Python の `docker` SDK は新しい依存として足さない（`subprocess` で CLI を呼ぶ。今の `run_command` と同じ形）
- mcp-server がコンテナの中で動くなら、ホストの `/var/run/docker.sock` を mcp-server にマウントする必要がある。mcp-server をホストで直接動かすなら（`cd mcp-server && uv run main.py`）マウントは要らない
- セッションのコンテナ: 名前は `dak-sandbox-` + `sha256(session_key)` の先頭 16 桁。起動コマンド:

  ```
  docker run -d --name <name> --label dak.sandbox=1 --network none
    --read-only --tmpfs /workspace:rw,exec,size=256m --tmpfs /tmp:rw,size=64m --env HOME=/workspace
    --cpus=<SANDBOX_CPUS> --memory=<SANDBOX_MEMORY> --pids-limit=<SANDBOX_PIDS_LIMIT>
    --cap-drop ALL --user nobody <SANDBOX_IMAGE> sleep infinity
  ```

  作業ディレクトリは tmpfs の `/workspace` で、コンテナを消せば中身も消える。`/workspace` と `/tmp` 以外は読み取り専用。
  Docker の `--tmpfs` は既定で `noexec` なので、`/workspace` には `exec` を付ける（付けないと `run_command("./build.sh")` が Permission denied になる）。
  `nobody` のホーム（`/nonexistent`）は無いので `HOME=/workspace` にする（`~` に書く CLI が動くように）
- ツールの実行: 全部 `docker exec -w /workspace <name> ...` でコンテナの中で動かす。`run_command` は今の `shell=True` と同じ意味を保つため `["sh", "-c", command]` を渡す（`shlex.split` にするとパイプやリダイレクトが効かなくなる）。ワークスペースはコンテナの中にしか無いので、ファイル系ツールも mcp-server のプロセスから `open()` できない。ファイル系ツールを `docker exec` で動かす部分は、ソケットの採否が決まってから別の Task にする（下の「決定」3）
- 利点: 本物のプロセス・ファイルシステム・ネットワークの分離。`--network none` / `--read-only` / `--tmpfs` / `--cpus` / `--memory` / `--pids-limit` / `--cap-drop ALL` / `--user` が全部使え、受け入れ条件 2（ネットワーク遮断と CPU・メモリ・PID の上限）を満たせるのはこの案だけ
- リスク: **Docker ソケットに触れるプロセスはホストの root と同じことができる**（`docker run --privileged -v /:/host ...` でホストのファイルシステム全体を読み書きできる）。ソケットをマウントした mcp-server で隔離の外のコード（`SANDBOX_MODE=off` / `inproc` の `run_command`）が動くと、`8001` に届く相手なら誰でもホストの root 相当になる。隔離コンテナ自身にはソケットを見せないので、`docker` モードの `run_command` からは届かない

### in-process 案（`SANDBOX_MODE=inproc`）

Docker を使わず、mcp-server のプロセスの中でセッションごとに作業ディレクトリを分ける。

- セッションごとに `tempfile.mkdtemp(prefix="dak-sandbox-<sha256(session_key) の先頭 16 桁>-")` の一時ディレクトリを作り（キーはヘッダの値そのままで、`/` や `..` を含みうるので、コンテナ名と同じくハッシュしてから使う）、ファイル系ツールの相対パスをそこで解決する。解決した実パス（`os.path.realpath`）がそのディレクトリの外に出るパス（絶対パス、`..`、外を指すシンボリックリンク）は拒否してエラーを返す。これで**ファイル系ツールどうしでは**別セッションのファイルが見えない
- `run_command` は `cwd` をそのディレクトリにして同じ `subprocess.run(..., shell=True)` で動かす。シェルは `cd /` も絶対パスも使えるので、`run_command` からは他のセッションのディレクトリも `/projects` も見える
- **真のプロセス・ネットワーク分離は提供できない**（同じ mcp-server のプロセス・同じユーザ・同じネットワーク名前空間で動く）。`resource.setrlimit` で子プロセスに CPU 時間・メモリ・FD 数の上限をかける案もあるが、この PBI では入れない: 上限は `run_command` の子プロセス 1 つずつにしか効かず（PID 数は利用者単位で、コンテナの root には効かない）、受け入れ条件 2 の「確認できる上限」にならないため。上限とネットワーク遮断は `docker` モードだけのものとする
- 利点: Docker ソケットが要らない。Docker の無い環境（CI、非 Docker の開発機）でも動き、単体テストで確かめられる
- リスク: 隔離はファイル系ツールの中だけの約束で、`run_command` を持つ相手には効かない。**セキュリティ境界として扱わない**（セッションどうしの取り違え・うっかりの上書きを防ぐもの）

## 既定値: `SANDBOX_MODE=off`

`SANDBOX_MODE` は `off` / `inproc` / `docker` の 3 値で、**既定は `off`**。

- `off` は今と同じ: 隔離を作らず、ファイル系ツールも `run_command` も共有の `/projects` をそのまま使う。SandboxManager は `workdir=None` を返し、呼び出し側はパスの解決も `subprocess.run` の呼び方も一切変えない
- 理由: `docker-compose.yml` は利用者の実リポジトリを `.:/projects` で渡しており、既存の呼び出し（`read_file("README.md")` など）と統合テストはそれを前提にしている。既定で隔離すると、既定の構成のまま全呼び出しが空のワークスペースに切り替わって壊れる。DAK の「暗黙の副作用を足さない」（`AGENTS.md`）にも合わせ、`inproc` / `docker` は明示の opt-in にする
- 不正な値は起動時にエラーで止める（黙って `off` に倒すと、隔離したつもりで隔離されていない）

## 決定（後続の Task が守ること）

1. **SandboxManager（`mcp-server/sandbox.py`、#283）**
   - 設定: `SANDBOX_MODE`（既定 `off`）、`SANDBOX_IMAGE`（既定 `python:3.12-slim`）、`SANDBOX_TTL_SECONDS`（既定 `900`）、`SANDBOX_CPUS`（既定 `1`）、`SANDBOX_MEMORY`（既定 `512m`）、`SANDBOX_PIDS_LIMIT`（既定 `128`）
   - `ensure_session(session_key)` で遅延生成し、同じキーは使い回す。`destroy_session(session_key)` で破棄（`docker rm -f` / `shutil.rmtree`）。`reap_expired(now)` で最後の利用から TTL を過ぎたものを破棄する。サーバの停止時に残りを全部破棄する `destroy_all()` も持つ（止めたサーバのコンテナを残さない）
   - 強制終了（OOM・SIGKILL）で `destroy_all()` が走らなかったコンテナは、メモリの表に無いので TTL でも消えず、同じキーの次の `docker run --name` が「名前が使われている」で失敗する。`docker` モードのコンテナには `--label dak.sandbox=1` を付け、起動時に `sweep()`（`docker ps -aq --filter label=dak.sandbox=1` の全部を `docker rm -f`）で消す。同じ Docker デーモンを `docker` モードの mcp-server 2 つで共有すると、後から起動した方が先の方のコンテナを消す。共有しないこととする
   - `subprocess.run` は差し替えられるようにし、単体テストは組み立てたコマンドと状態の遷移だけを見る（実際のコンテナは #285）
   - 許可・拒否の判断は持たない
2. **配線（`mcp-server/main.py`、#284）**
   - ツール関数に `ctx: Context | None = None` を足し、`X-DAK-Session-Key` を読む（無ければ `default`。ヘッダを送らない相手どうしは同じ隔離を共有する）
   - `workdir is None`（`off`）なら今のコードの経路をそのまま通す
   - `inproc`: ファイル系ツールは上の「in-process 案」の閉じ込めで解決し、`run_command` は `cwd=workdir`
   - `docker`: `run_command` は `exec_in_session`（`docker exec -w /workspace <name> sh -c <command>`）。ファイル系ツールは 3 の Task ができるまで「`docker` モードでは未対応」のエラーを返す（黙って mcp-server の側のファイルを触らない）
   - TTL の破棄: `lifespan` の起動時に `sweep()` し、`min(SANDBOX_TTL_SECONDS, 60)` 秒ごとに `reap_expired()` を呼ぶループを回し、停止時に `destroy_all()` する。呼び出しが来ないときも TTL 後に破棄される
   - 「最後の利用」は呼び出しの始まり（`ensure_session`）の時刻。それでも実行中の呼び出しのセッションは消えない: ツールの本体は `async def` の中で同期の `subprocess.run` / ファイル I/O をするので、その間 event loop は塞がり、`lifespan` の reaper は呼び出しの合間にしか走らない。ツールをスレッドや非同期の実行に変えるなら、実行中の呼び出しの数を持って reaper に飛ばさせる
   - `docker-compose.yml` は、利用者がソケットのマウントを承認するまで変えない（#284 の「着手前に確認」）。`mcp-server/Dockerfile` には `sandbox.py` のコピーと `docker` CLI を足す（CLI だけではホストに届かない。届くのはソケットをマウントしたときだけ）
3. **ソケットを承認されたら**（`## 判断待ち` の A）
   - ソケットのマウントは基本の `docker-compose.yml` に入れず、opt-in の上書きファイル（`docker-compose.sandbox.yml`。`SANDBOX_MODE=docker` とソケットを一緒に設定する）に置く。基本の構成でソケットが見えることは無い
   - mcp-server は、ソケット（`/var/run/docker.sock`）が見えるのに `SANDBOX_MODE` が `docker` でなければ起動を拒む（上の「リスク」: 隔離の外の `run_command` がホストの root 相当になる組み合わせを作らない）
   - `docker` モードのファイル系ツールを `docker exec` で動かす Task を切る
   - #285 の `docker` モードの実機検証（2 セッションの不可視、ネットワーク遮断、`docker inspect` の `NanoCpus` / `Memory` / `PidsLimit`、TTL 後の `docker ps`）を行う
4. **検証（#285）**: `inproc` の不可視と破棄は `cd mcp-server && uv run pytest -q` で確かめる（Docker 不要）。`docker` モードは Docker のデーモンに届く実機でだけ確かめられ、明示の環境変数（`DAK_SANDBOX_DOCKER_TESTS=1`）が無ければ `pytest.mark.skipif` で飛ばす（承認のあとにコードを直さずに回せるように。理由の文に「未承認なら回さない」と書く）。`tests/integration/` が通っても `docker` モードを確かめたことにはならない（`permission-boundary.md` の「#20 への制約」）

## 利用者の判断（#20 の `## 判断待ち`）

問い: mcp-server に Docker ソケットを見せて `docker` モードを使えるようにするか。

- A: opt-in の上書きファイルでだけマウントする（決定 3）。基本の構成は変えない。その上書きファイルで起動した mcp-server は、`8001` に届く相手と agent に、隔離コンテナを作らせる権限（= ホストの Docker デーモン）を渡す。起動を拒む検査で、隔離の外の `run_command` と組み合わせないようにする
- B: compose ではマウントしない。`docker` モードは mcp-server をホストで直接動かしたときだけ使える。compose では `off` / `inproc` だけ（受け入れ条件 2 は compose の構成では満たさない）
- C: `docker` モードをやめ、`inproc` だけにする。受け入れ条件 2 は満たさない（PBI のスコープを変える）

どれを選んでも、#283 と #284 のうち `off` / `inproc` の部分は同じなので、回答を待たずに進める（#20 の決定ログ 2026-09-30）。

## 未検証事項

- 確かめたこと（2026-09-30）: `SANDBOX_MODE=inproc` の mcp-server を uvicorn で起動し、`X-DAK-Session-Key` の異なる 2 つの MCP クライアント（`mcp` の `streamablehttp_client`）から呼んだ。ヘッダは `ctx.request_context.request.headers` で取れ、互いのファイルは見えず、`../` は拒まれ、TTL のあと作業ディレクトリは消えた（#284 の PR #444）。inproc の不可視と破棄は `mcp-server/tests/` の単体テストでも確かめる（#285）
- `docker` モードの隔離（上のフラグが実際に効くこと、TTL の破棄）は動かしていない（#285、承認後）
- rootless の Docker では、ソケットに触れて得られるのはそのデーモンを動かすユーザの権限で、上の「リスク」（ホストの root 相当）より小さい。一方で `--cpus` / `--pids-limit` は cgroup v2 の委譲が無いと効かない。どちらも実機で確かめていない
