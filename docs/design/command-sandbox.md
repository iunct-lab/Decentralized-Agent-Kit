# run_command のサンドボックス: コンテナの中で srt を動かす構成

PBI #292 / Task #293。mcp-server の `run_command` を anthropics/sandbox-runtime（`srt`）で包み、書き込みを `/projects` と `/tmp`、通信を許可リストに閉じ込めるための前提を確かめた結果。
後続の Task（#294 / #295）は、ここで利用者が選んだ構成で作る。**どの構成を採るかは PBI #292 の決定ログにある利用者の回答が正**で、この文書は候補と事実を並べる。

## 1. 結論

- Docker の既定のまま（seccomp も `/proc` のマスクもそのまま）では、root でも非 root でも srt は動かない。bubblewrap が名前空間を作れない（下の表の (a) (b) (e)）
- `--privileged` なしで動くのは、**seccomp を緩めたうえで、`/proc` の扱いをもう 1 つ緩めた**組み合わせだけ:
  - seccomp: Docker の既定プロファイルに名前空間と mount 系の 13 の syscall を足した独自プロファイル（(c)）、または `seccomp=unconfined`（(d)）
  - `/proc`: コンテナの `/proc` のマスクを外す `--security-opt systempaths=unconfined`、または srt の `enableWeakerNestedSandbox: true`
- 動いた構成では、書き込みの拒否・読み取りの拒否・許可リスト外への通信の拒否・許可した宛先への通信が、どれも期待どおりだった（3 節）
- **包む単位は `run_command` ごと**。サーバ全体を包むと、srt がネットワーク名前空間を外すため、`allowLocalBinding: true` にしても他のコンテナからも同じコンテナの中からも `:8000` に届かない（4 節）
- コマンド 1 回あたりの起動の遅れは中央値 約 470 ms（5 節）。`run_command` のタイムアウト 60 秒に対しては問題にならない
- イメージは非圧縮で 371 MB → 822 MB（+451 MB。Node.js・npm・srt・bubblewrap・socat・ripgrep・curl）

推す構成は 6 節。

## 2. 試した環境と版

| 項目 | 値 |
|------|----|
| ホスト | Linux（Amazon Linux 2023、カーネル 6.18、arm64）。AppArmor なし |
| Docker | 29.8.1、**rootless**（`docker info` の SecurityOptions: `seccomp` builtin / `rootless` / `cgroupns`） |
| ベースイメージ | `python:3.10-slim`（Debian 13.7）。mcp-server の Dockerfile をそのままビルドしたものの上に足した |
| srt | `@anthropic-ai/sandbox-runtime@0.0.77`（2026-09-18 公開。2026-10-01 時点の最新 0.0.78 は公開から 1 日で、依存の最小経過日数を満たさないので採らない） |
| apt で足したもの | `nodejs` 20.19.2、`npm`、`bubblewrap` 0.12.0、`socat` 1.8.0.3、`ripgrep` 14.1.1（srt は Node.js 20.11 以上が要る）。試験のために `curl` も足した |

**未検証**: rootful の Docker、Docker Desktop、AppArmor のあるホスト（Ubuntu 24.04 以降は `kernel.apparmor_restrict_unprivileged_userns` が既定で有効。srt の README を参照）、x86_64。rootless と rootful ではコンテナの root と非 root の意味が違う（6 節のリスク）。

試験用の Dockerfile（書き捨て。リポジトリには入れていない）:

```dockerfile
FROM <mcp-server をビルドしたイメージ>
RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm bubblewrap socat ripgrep curl \
 && npm install -g @anthropic-ai/sandbox-runtime@0.0.77 \
 && npm cache clean --force && rm -rf /var/lib/apt/lists/*
RUN useradd -m -u 1000 probe
RUN mkdir -p /srv/deny && echo secret > /srv/deny/secret && chmod -R a+rX /srv/deny
```

srt の設定（`srt-weak.json` は末尾に `"enableWeakerNestedSandbox": true` を足したもの）:

```json
{
  "network": { "allowedDomains": ["example.com"], "deniedDomains": [], "allowLocalBinding": false },
  "filesystem": { "denyRead": ["/srv/deny"], "allowWrite": ["/projects", "/tmp"], "denyWrite": [] }
}
```

## 3. 構成ごとの結果

実行したのは `docker run --rm [オプション] -v <書き捨ての dir>:/projects <試験用イメージ> srt --settings <設定> /w/probe.sh`（すべて `--privileged` なし）。`probe.sh` は srt の中で次を 1 つずつ試す:
`/projects` と `/tmp` への書き込み、`/etc` と `/app` への書き込み、`denyRead` に入れた `/srv/deny/secret` の読み取り、`curl https://example.com`（許可）、`curl https://example.org`（許可リスト外）、プロキシを通さない直接の TCP 接続と `curl --noproxy '*'`。

| 構成 | Docker のオプション | ユーザ | 結果 |
|------|------|------|------|
| (a) 既定のまま | なし | root | 動かない: `bwrap: No permissions to create a new namespace` |
| (b) 既定のまま | なし | 1000:1000 | 動かない: 同上 |
| (c) 独自 seccomp | `seccomp=<独自プロファイル>` | root / 1000 | 動かない: `bwrap: Can't mount proc on /proc: Operation not permitted` |
| (d) seccomp・AppArmor を外す | `seccomp=unconfined`, `apparmor=unconfined` | root / 1000 | 動かない: 同上 |
| (e) 既定 + 弱いモード | なし（srt 側で `enableWeakerNestedSandbox: true`） | root / 1000 | 動かない: (a) と同じ |
| 既定 seccomp + `systempaths` | `systempaths=unconfined` | root / 1000 | 動かない: (a) と同じ（`/proc` のマスクを外しても、seccomp が名前空間の作成を止める） |
| (c) + `systempaths` | `seccomp=<独自>`, `systempaths=unconfined` | root / 1000 | **動く**。全項目が期待どおり |
| (c) + 弱いモード | `seccomp=<独自>` + `enableWeakerNestedSandbox` | root / 1000 | **動く**。全項目が期待どおり。ただし `/proc` を共有する（下） |
| (d) + `systempaths` | `seccomp=unconfined`, `apparmor=unconfined`, `systempaths=unconfined` | root / 1000 | **動く**。全項目が期待どおり |
| (d) + 弱いモード | `seccomp=unconfined`, `apparmor=unconfined` + `enableWeakerNestedSandbox` | root / 1000 | **動く**。全項目が期待どおり。`/proc` を共有する |

動いた 4 つの組み合わせでの `probe.sh` の出力（どれも同じ）:

```text
write /projects: ok
write /tmp: ok
write /etc (expect FAIL): FAIL (touch: cannot touch '/etc/ng': Read-only file system )
write /app (expect FAIL): FAIL (touch: cannot touch '/app/ng': Read-only file system )
read /srv/deny/secret (expect FAIL): FAIL (cat: /srv/deny/secret: No such file or directory )
curl example.com (allowed): ok
curl example.org (expect FAIL): FAIL (curl: (56) CONNECT tunnel failed, response 403 )
direct TCP example.org:443 bypassing proxy (expect FAIL): FAIL (... create_connection raise ...)
curl --noproxy example.com (expect FAIL): FAIL (curl: (6) Could not resolve host: example.com )
```

srt を通さないで同じ `probe.sh` を流すと、全項目が成功する（直接の TCP 接続も通る）。

- `denyRead` に入れたディレクトリは、srt の中では空の tmpfs になる（読み取りは「無い」で失敗する）
- 通信は、srt がネットワーク名前空間を外し、ホスト側（srt の外）のプロキシだけを通す。プロキシを無視するプログラムは名前解決もできずに失敗する
- AppArmor はこのホストに無いので、`apparmor=unconfined` は結果に影響していない
- `allowWrite` に無いパスは srt の中では読み取り専用になる。**`run_command` は今 `cwd` を指定せずに実行するので、作業ディレクトリは mcp-server の `WORKDIR` の `/app`**（`main.py` の `subprocess.run(..., shell=True)`）。srt で包むと、相対パスに書くコマンド（`echo x > out.txt` など）は `Read-only file system` で失敗するようになる。#294 で、有効時の作業ディレクトリを `/projects` にするか、`/app` を `allowWrite` に足すかを決める（`/app` には mcp-server 自身のコードと `.venv` があるので、足すとコマンドがサーバのコードを書き換えられる）

### 弱いモード（`enableWeakerNestedSandbox`）で何が弱くなるか

srt 0.0.77 の実装（`dist/sandbox/linux-sandbox-utils.js`）では、通常は bubblewrap に `--proc /proc`（新しい `/proc` をマウント）を渡し、弱いモードでは代わりに `--bind /proc /proc`（コンテナの `/proc` をそのまま見せる）を渡す。違いはこれだけ。確かめたこと:

- 弱いモードでは、srt の中からコンテナの他のプロセスが `/proc` に見える（`/proc` の数字のエントリが 17〜18 件。`systempaths` の構成では 5〜7 件で、srt の中のプロセスだけ）。他のプロセスのコマンドライン（`/proc/<pid>/cmdline`）が読める
- 他のプロセスの環境変数（`/proc/<pid>/environ`）は、どちらのモードでも `Permission denied`
- PID 名前空間は分かれているので、コンテナの他のプロセスに `kill` は届かない（`No such process`）

srt の README は弱いモードを「隔離がかなり弱まる。他の手段で隔離されている場合だけ使う」と書く。

### 独自 seccomp プロファイル（(c)）の差分

moby の既定プロファイル（`moby/profiles` の `seccomp/default.json`、コミット `6fe7deb1b9fb`、2026-09-17）からの差分は 2 点:

1. `clone3` を `CAP_SYS_ADMIN` が無いと `ENOSYS` にする規則（`"names": ["clone3"], "action": "SCMP_ACT_ERRNO"`）を消す
2. 次の規則を足す（既定では `CAP_SYS_ADMIN` があるときだけ許される syscall を、cap なしで許す）:

```json
{
  "names": ["unshare", "clone", "clone3", "setns", "mount", "umount2", "pivot_root",
            "mount_setattr", "open_tree", "move_mount", "fsopen", "fsmount", "fsconfig"],
  "action": "SCMP_ACT_ALLOW"
}
```

`seccomp=unconfined`（(d)）と比べ、`bpf`・`perf_event_open`・`keyctl`・`ptrace` などその他の制限は既定のまま残る。

## 4. サーバ全体を包む案

`srt --settings <設定> -c "/app/.venv/bin/python main.py"` で mcp-server を起動した（構成は (c) + `systempaths`、root）。uvicorn は `Uvicorn running on http://0.0.0.0:8000` と出して起動するが:

| 試したこと | `allowLocalBinding: false` | `allowLocalBinding: true` |
|------|------|------|
| 同じ Docker ネットワークの別のコンテナから `POST http://<コンテナ>:8000/mcp` | `curl: (7) Failed to connect ... Could not connect to server` | 同じ |
| `docker exec` で同じコンテナの srt の外から `POST http://127.0.0.1:8000/mcp` | 同じ | 同じ |
| 参考: srt なしで起動して別のコンテナから | HTTP 421（届いている。Host ヘッダの検査で返る） | — |

srt の README のとおり、Linux では包んだプロセスのネットワーク名前空間を外すので、受信はどこからも届かない。`allowLocalBinding` は名前空間の中で bind できるようにするだけで、外からの受信は通さない。
したがってサーバ全体は包めない。`run_command` ごとに包む（PBI #292 の第一案のまま）。`read_file` / `write_file` などのファイル系ツールは srt の枠の外に残る（#292 のスコープ外の節のとおり、#109 の保護パスと #20 の隔離で扱う）。

## 5. `run_command` ごとに包むときの遅れ

各 10 回、`date +%s%N` で測った（ホストは 2 コア・メモリ 3.7 GB）:

| 構成 | `sh -c true` | `srt --settings <設定> true` |
|------|------|------|
| (c) + `systempaths`、1000:1000 | 中央値 1.0 ms（1〜2） | 中央値 466.5 ms（441〜570） |
| (c) + 弱いモード、1000:1000 | 中央値 1.0 ms（1〜2） | 中央値 480.0 ms（448〜570） |

遅れの大半は srt（Node.js）の起動とプロキシの立ち上げ。コマンドごとに約 0.5 秒増えるが、`run_command` のタイムアウト（60 秒）に対しては小さい。モデルの 1 ターンの待ち時間に比べても目立たない見込み。

## 6. 候補と推す構成（利用者が選ぶ）

どの候補もコンテナ自体の防御を Docker の既定より下げる。下げた分を srt の枠（コマンドの書き込み・読み取り・通信の制限）で取り返す、という取引になる。

| 候補 | 下げるもの | srt の枠の強さ | 主なリスク |
|------|------|------|------|
| **1. (c) + `systempaths=unconfined`、非 root（推す）** | seccomp の 13 syscall、コンテナの `/proc` のマスクと読み取り専用 | 強い（srt の中は自分の `/proc` だけ） コンテナ全体で `/proc` のマスクと読み取り専用が外れる。srt の枠の外に残る mcp-server のファイル系ツール（`read_file` など）からも、マスクされていたパスを開ける（下の注意）。名前空間を作れることで、カーネルの攻撃面が増える |
| 2. (c) + 弱いモード | seccomp の 13 syscall | 弱い（srt の中からコンテナの他のプロセスとコマンドラインが見える） | srt の README が「かなり弱まる」と書くモード。コンテナの `/proc` のマスクは残る |
| 3. (d) + `systempaths` か弱いモード | seccomp と AppArmor を全部 | 1 か 2 と同じ | seccomp の制限が全部外れる。1・2 より広く下げる理由が見当たらない |
| 4. 見送る | — | — | `run_command` は今のまま（OS のレベルでは止めない）。#294 / #295 はやらない |

推す理由（候補 1）: 動いた中で srt の枠が一番強く、seccomp の緩め方も最小。弱いモードは srt 自身が非推奨と書く。

候補 1 の注意:

- **srt の外のファイル系ツールから見える `/proc`**: `read_file` などは mcp-server のプロセスの中で動き、srt で包まれない（4 節）。非 root（1000:1000）で確かめた結果、既定の Docker では `/proc/kcore`・`/proc/timer_list` は `/dev/null` で覆われていて中身は空、`/proc/sys` は読み取り専用。`systempaths=unconfined` にすると、どちらも覆いが外れるが、ファイルの権限（root だけが読める・書ける）で `Permission denied` になった（`/proc/kcore`・`/proc/timer_list`・`/proc/sched_debug` の読み取り、`/proc/sys/kernel/hostname` への書き込み）。root で動かすと、この権限の壁は無くなる
- **root で動かさない**。rootful の Docker ではコンテナの root はホストの root と同じ uid で、`systempaths=unconfined` で読み取り専用が外れた `/proc/sys`・`/proc/sysrq-trigger` などに書ける余地が増える。rootful では試していない
- 非 root にすると、`/projects` にマウントしたホストのファイルに書けるかが uid の対応で変わる。rootful の Docker では compose の `user:` にホストの利用者の uid:gid を入れる。rootless の Docker では、コンテナの uid 1000 はホストの別の uid（subuid）になるので、ホストの利用者のファイルには書けない（試験では書き捨ての dir を `chmod 777` にして確かめた）。#295 でどう扱うかを決める
- 非 root にすると、mcp-server 自身も非 root で起動することになる。1000:1000 で `uv run python -c "print(1)"` が `/app` で動くこと（root の持つ `/app/.venv` を読むだけで、書き込みは要らない）は確かめた。サーバそのものを非 root で起動して agent から呼べるかは #295 で確かめる
- イメージに足すもの（+451 MB）は、既定のイメージには入れず、ビルド引数で有効にしたときだけ入れる（PBI #292 の決定ログのとおり）
