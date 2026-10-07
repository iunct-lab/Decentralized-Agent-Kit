# コード実行の隔離方式の比較（#20 の実行先を選ぶ材料）

PBI #296 / Task #297。#20 の `SandboxManager`（`session-sandbox.md`）は、`docker` CLI でセッションごとに runc のコンテナを起動する。runc のコンテナはホストとカーネルを共有するので、カーネルの脆弱性を突かれると隔離が破られる。
この文書は、カーネルから分ける方式（gVisor、Kata Containers、Docker Sandboxes、CubeSandbox）と、プロセス単位の OS サンドボックス（srt、vetto）を一次資料で比べ、実測の結果（#298）と推す案（#300）を書く。
**どの方式を採るかは PBI #296 の決定ログにある利用者の回答が正**で、この文書は事実と候補を並べる。

調べた日: 2026-10-07。根拠は各プロジェクトの公式文書・リポジトリ・GitHub のリリースだけ。確かめられなかった項目は「未確認」と書く。

## 1. 比較表

「`SandboxManager` に足す変更」は、今の `docker run -d … sleep infinity` → `docker exec` → `docker rm -f` の組み立て（`mcp-server/sandbox.py`）に対する変更。
「独立コンテナの原則」は、mcp-server のコンテナから標準の口（Docker の API）だけで使えるか。今の docker モードは、ホストの Docker ソケットを mcp-server に渡して使う（`session-sandbox.md` の「Docker ソケット案」）。

| 方式 | 隔離の仕組み | 動く条件 | DAK からの呼び出し方 | `SandboxManager` に足す変更 | 独立コンテナの原則 | ライセンス・費用・アカウント | 版（日付） | 一次資料 |
|---|---|---|---|---|---|---|---|---|
| runc（今の前提） | ホストのカーネルを共有する OCI ランタイム。名前空間・cgroups・seccomp で分ける | Linux | `docker run`（既定のランタイム） | なし | 合う（今の形） | Apache-2.0・無料・不要 | v1.5.2（2026-09-25） | [README](https://github.com/opencontainers/runc) 、[releases](https://github.com/opencontainers/runc/releases) |
| gVisor（`runsc`） | サンドボックスごとの application kernel がアプリのシステムコールを受け、ホストのカーネルに直接届くものを減らす（VM の仮想ハードウェアは持たない）。既定の platform は systrap で、KVM は任意 | Linux 5.6 以上、x86_64 / ARM64。KVM は不要 | `docker run --runtime=runsc`。ホストの Docker に `runsc install` で登録する | `docker run` に `--runtime=<値>` を 1 つ足す | 合う。ランタイムの登録はホストの Docker の設定で、mcp-server は名前を渡すだけ | Apache-2.0・無料・不要 | release-20260928.0（2026-09-30） | [overview](https://gvisor.dev/docs/) 、[install](https://gvisor.dev/docs/user_guide/install/) 、[docker](https://gvisor.dev/docs/user_guide/quick_start/docker/) 、[platforms](https://gvisor.dev/docs/architecture_guide/platforms/) 、[compatibility](https://gvisor.dev/docs/user_guide/compatibility/) 、[releases](https://github.com/google/gvisor/releases) |
| Kata Containers | コンテナごとに軽量 VM（QEMU / Cloud Hypervisor / Dragonball）を起動し、ゲストのカーネルで動かす | ハードウェア仮想化（KVM）。「nested virtualization か bare metal が要る」。x86_64 / aarch64 / ppc64le / s390x | `docker run --runtime io.containerd.kata.v2`（Docker 22.06 以上） | `--runtime=<値>` を 1 つ足す（gVisor と同じ） | 合う（gVisor と同じ） | Apache-2.0・無料・不要 | 4.2.0（2026-09-15） | [README](https://github.com/kata-containers/kata-containers) 、[install](https://github.com/kata-containers/kata-containers/blob/main/docs/install/README.md) 、[Limitations](https://github.com/kata-containers/kata-containers/blob/main/docs/Limitations.md) 、[releases](https://github.com/kata-containers/kata-containers/releases) |
| Docker Sandboxes（`sbx`） | サンドボックスごとに microVM と専用の Docker デーモン。外向きの TCP はホストのプロキシを通り、そこでポリシーをかける | macOS Sonoma 14 以上の Apple silicon、Ubuntu 24.04 以上で KVM の使える Linux（x86_64 / arm64）、Windows 11（x64） | ホストの `sbx` CLI: `sbx create shell` → `sbx exec` → `sbx rm`。ローカルの API・ソケットの文書は見当たらない（未確認） | `docker` の代わりに `sbx` を呼ぶ別の実装。`docker exec -d` 相当は無い | 合わない見込み。`sbx` はホストの CLI で、mcp-server のコンテナから呼ぶ標準の口が文書に無い（3 節） | プロプライエタリ（Docker Inc.）。CLI とローカルの実行は商用でも無料。`sbx login`（Docker アカウント）が要る | v0.47.0（2026-10-05）。v0.48.0-rc3 は rc | [overview](https://docs.docker.com/ai/sandboxes/) 、[install](https://docs.docker.com/ai/sandboxes/install/) 、[architecture](https://docs.docker.com/ai/sandboxes/architecture/) 、[CLI](https://docs.docker.com/reference/cli/sbx/) 、[FAQ](https://docs.docker.com/ai/sandboxes/faq/) 、[releases](https://github.com/docker/sbx-releases) |
| CubeSandbox | サンドボックスごとに KVM の microVM（RustVMM）と専用のカーネル。サンドボックス間と外向きの通信は eBPF の仮想スイッチと L7 のゲートウェイで絞る | KVM の使える x86_64 / aarch64 の Linux。KVM の無い x86_64 のクラウドの VM でも、配布の PVM のホストカーネルを入れて起動し直せば動く（PVM は x86_64 だけ。aarch64 は KVM の使える bare metal が要る）。4 コア・8 GB 以上、XFS の `/data/cubelet` に 50 GB 以上 | E2B 互換の REST API（CubeAPI）。API の前にクラスタ管理・ノード管理・ハイパーバイザ・containerd の shim の一式を置く | `docker` を呼ばない別の実装（E2B 互換の API のクライアント） | API で呼ぶので合うが、ホスト側に常駐サービスの一式が要る | Apache-2.0（同梱の第三者のものを除く）・無料。アカウント不要（ローカルの配置では E2B の SDK が求める `E2B_API_KEY` に任意の文字列を入れる） | v0.7.2（2026-09-24） | [README](https://github.com/TencentCloud/CubeSandbox) 、[quickstart](https://github.com/TencentCloud/CubeSandbox/blob/master/docs/guide/quickstart.md) 、[releases](https://github.com/TencentCloud/CubeSandbox/releases) |
| srt（プロセス単位の層） | コンテナを使わず、Linux では bubblewrap で名前空間を作り、通信はホストのプロキシに通す。macOS は Seatbelt | Linux（bubblewrap 0.4 以上、socat、ripgrep）、macOS。KVM 不要 | `srt [--settings <file>] <command>` でコマンドを包む | 別の層。#292 で `run_command` を包む（`command-sandbox.md`） | 合う（コンテナの中で使える構成は `command-sandbox.md`） | Apache-2.0・無料・不要。「Beta Research Preview」 | 0.0.78（2026-09-30） | [README](https://github.com/anthropics/sandbox-runtime) 、[npm](https://registry.npmjs.org/@anthropic-ai/sandbox-runtime) |
| vetto（プロセス単位の層） | デーモンを持たない非特権のプロセスのサンドボックス。Linux では Landlock・名前空間・seccomp・cgroups v2 の `cgroup.kill` と、許可リストのためのループバックのプロキシ。仕組みが欠けていれば exit 125 で止まる | Linux 5.13 以上（x86_64 / aarch64）、macOS、Windows。KVM 不要 | `vetto run -- <command>` | 別の層（srt と同じ） | 合う見込み（コンテナの中で動くかは未確認） | Apache-2.0・無料・不要 | v0.6.1（2026-10-06）。リポジトリの作成は 2026-08-22 | [README](https://github.com/shleder/vetto) 、[compat](https://github.com/shleder/vetto/blob/main/docs/compat.md) 、[releases](https://github.com/shleder/vetto/releases) |

## 2. DAK で使うときの要点

- **runc**: 今の docker モードの前提。カーネルの脆弱性に対する防御は無い。`--cpus` / `--memory` / `--pids-limit` は cgroups が使える Docker でしか効かない（cgroups の無い rootless の Docker では効かない。`docker info` の警告で分かる）
- **gVisor**: `SandboxManager` への変更が最小（`--runtime` を足すだけ）で、KVM の無いクラウドの VM や arm64 でも動く。代わりにホストの Docker に `runsc` を入れて登録する作業が要り、互換性に制限がある（公式の互換性の文書: sandbox の中では cgroup の上限を強制しない、ext4 / fat32 のブロックデバイスは使えない、io_uring は既定で無効、など）。#283 の引数がどこまで効くかは 4 節で実測する
- **Kata Containers**: 呼び出し方は gVisor と同じで、VM の境界で分けるので隔離は最も強い部類。代わりに KVM（入れ子の仮想化か bare metal）が要る。公式は Kubernetes 向けの導入（Helm の chart）を推し、Docker では `io.containerd.kata.v2` の shim を入れる。rootless の Docker で動くかは未確認
- **Docker Sandboxes**: エージェント向けの製品で、`sbx create shell` でエージェントを起動しない汎用のサンドボックスも作れる。ネットワークは `sbx policy`（全体の既定 `allow-all` / `balanced` / `deny-all`、サンドボックスごとの allow / deny）で絞る。ホストの CLI がローカルの `sandboxd` を操作する作りで、ローカルの API は文書に無い。REST API と SDK はクラウドのサンドボックス向けで experimental（有料の契約が要る）。評価の詳細は 3 節
- **CubeSandbox**: E2B 互換の API を呼ぶだけで済むが、ホストに一式（API ゲートウェイ、クラスタ管理、ノード管理、ハイパーバイザ、eBPF の仮想スイッチ、外向きのゲートウェイ）を常駐させる。KVM（x86_64 なら PVM のホストカーネルでも可。その場合はホストのカーネルを入れ替える）が要り、XFS の 50 GB の領域など条件が重い。この比較ではそのホストを用意しないので、実測しない（PBI #296 のスコープ外）
- **srt / vetto**: カーネルを分けない。コマンドごとに包む層で、#20 の実行先（コンテナの種類）とは独立に重ねられる。srt は #292 で扱う。vetto は #31 のコメントで作者本人（リポジトリの所有者と同じ GitHub のアカウント、2026-08-30）が紹介したもので、第三者の評価は見当たらない。比較表に載せるだけにし、個別の検証はしない

## 3. Docker Sandboxes を mcp-server から使うには（評価、実測しない）

実測はしない（PBI #296 の決定ログ）。動くホストが Apple silicon の Mac、KVM の使える Ubuntu 24.04 以上、Windows 11 に限られ、`sbx login` に Docker アカウントのサインインが要るため。下は 2026-10-07 時点の公式の文書（v0.47.0）から読めることと、そこからの評価。

### 作成・実行・破棄の手順（文書から）

| 段階 | コマンド | 文書から読めること |
|---|---|---|
| 準備（1 回だけ） | `sbx login`、`sbx policy init <allow-all\|balanced\|deny-all>` | 全体のネットワークの方針は、最初のサンドボックスを作る前に 1 回決める（[policy init](https://docs.docker.com/reference/cli/sbx/policy/init/)） |
| 作成 | `sbx create shell [PATH...]` | エージェントを起動しない汎用のサンドボックスを作れる。パスを省けばホストのファイルをマウントしない（[create shell](https://docs.docker.com/reference/cli/sbx/create/shell/)）。作成時に `--deny-network` でそのサンドボックスの拒否の規則を足せる |
| 通信の制限 | `sbx policy allow\|deny network [--sandbox <name>] <host,cidr…>` | 全体またはサンドボックスごとに許可・拒否を足す（[policy allow network](https://docs.docker.com/reference/cli/sbx/policy/allow/network/)）。外向きの TCP はすべてホストのプロキシを通り、そこで方針をかける（[architecture](https://docs.docker.com/ai/sandboxes/architecture/)） |
| 実行 | `sbx exec [-u <user>] [-w <dir>] <sandbox> <command>` | 振る舞いは `docker exec` に合わせてある。止まっていれば先に起動する。`-d`（切り離し）は使えない（[exec](https://docs.docker.com/reference/cli/sbx/exec/)） |
| 破棄 | `sbx rm [-f] <sandbox>`、`sbx ls --json` | 中のもの（イメージ、入れたパッケージ、作ったファイル）は消すまで残る（[rm](https://docs.docker.com/reference/cli/sbx/rm/)、[architecture](https://docs.docker.com/ai/sandboxes/architecture/)） |

`SandboxManager` の `docker run -d` → `docker exec` → `docker rm -f` には、`sbx create shell` → `sbx exec` → `sbx rm -f` がそのまま対応する。#283 の引数のうち、`--network none` はサンドボックスごとの明示の拒否の規則（`sbx create shell --deny-network …` か `sbx policy deny network --sandbox <name> …`）で置き換える見込み。全体の方針（`deny-all` を含む）だけでは足りない。kit がサンドボックスごとに足す許可の規則は `deny-all` の下でも効き、方針は明示の拒否ではないので、それを上書きしない。拒否の規則は許可より優先する。また `balanced` と `deny-all` では、どの規則にも合わない通信は拒否ではなく利用者の承認待ちになる（[local policy](https://docs.docker.com/ai/sandboxes/governance/access-controls/local/)）。無人で使うなら、全部の宛先を拒否する規則の書き方（ワイルドカードの拒否が通るか）を確かめる必要がある（未確認）。`--cpus` / `--memory` は `sbx create` の同名のオプション（`--memory` の最小は 512 MiB）で置き換える見込み。`--read-only`、`--cap-drop`、`--pids-limit` に当たるものは文書に見当たらない（未確認）。`--cpus` は 0（ホストの全 CPU）、`--memory` はホストのメモリの 50%（512 MiB〜32 GiB）が既定。隔離の要は、サンドボックスごとに専用のカーネルを持つ microVM で、中の利用者は sudo のできる非 root（[isolation](https://docs.docker.com/ai/sandboxes/security/isolation/)）。

### mcp-server のコンテナから呼ぶ方法

`sbx` はホストで動く CLI で、ホストの `sandboxd` を操作する（[daemon](https://docs.docker.com/reference/cli/sbx/daemon/)）。ローカルの `sandboxd` の API やソケットの仕様は文書に無い。REST API と SDK はあるが、クラウドのサンドボックス向けで experimental（[Sandboxes API](https://docs.docker.com/ai/sandboxes-api/)）。mcp-server のコンテナから使う方法は次の 3 つが考えられる（どれも試していない）:

| 方法 | ホスト側に要るもの | 独立コンテナの原則 | #16 の権限境界 |
|---|---|---|---|
| A. ホストに橋渡しのサービスを置き、mcp-server は HTTP で「作る・実行する・消す」を頼む。サービスがホストの `sbx` を呼ぶ | 新しい常駐サービス（DAK の外か、新しいコンポーネント）、`sbx login` 済みのホストの利用者 | 標準の口（HTTP）だけでつながるので合う。ただし部品が 1 つ増える | 橋渡しのサービスが「何を実行してよいか」を判断しないように作る必要がある（判断は agent の `before_tool_callback`） |
| B. `sandboxd` のソケットを mcp-server のコンテナに渡し、コンテナの中の `sbx` から使う | ソケットの場所と、コンテナの中で `sbx` が使えるか（文書に無い） | 文書に無い内部の口に依存するので合わない | ソケットを持つ者はホストのサンドボックスを全部操作できる（今の Docker ソケット案と同じ種類の権限） |
| C. mcp-server をコンテナに入れず、ホストで直接動かす（`cd mcp-server && uv run main.py`） | ホストに `sbx` と Python の環境 | mcp-server だけがコンテナの外になる。今の docker モードでも許している形（`session-sandbox.md`） | 変わらない |

KVM の使える Ubuntu 24.04 以上の Linux なら、mcp-server と同じホストで `sbx` を動かせる（要件の上では）。macOS では、mcp-server を動かす Docker（Colima や Docker Desktop の VM の中）と `sbx` の microVM が別の層にあるので、A か C になる。

### 評価

- `SandboxManager` から見た手順は docker モードとほぼ同じ形で置き換えられる。一方で、呼び出し先がホストの CLI になり、コンテナの中から使う標準の口が無い。docker モードの「Docker ソケットを渡す」形（`--runtime` を足すだけの gVisor・Kata）に比べ、構成の変更が大きい
- 運用の負担: `sbx login`（Docker アカウント）、全体のネットワークの方針の初期化、ホストに常駐する `sandboxd`。ライセンスはプロプライエタリで、ローカルの利用は無料（[FAQ](https://docs.docker.com/ai/sandboxes/faq/)）
- 利点: microVM ごとの専用のカーネルと Docker デーモン、ホストのプロキシでの通信の制御と資格情報の差し込み。開発者の Mac で動く唯一の microVM の選択肢
- 実測は未検証。採る方向になったら、作成から最初の `sbx exec` まで、`sbx exec` 1 回、破棄、4 節と同じ Python の処理を、上の要件を満たすホストで測る

## 4. 実測

### 共通の条件

- 計測先: Docker が root で動き cgroups の使える、使い捨ての arm64 の Linux（構成は下の表に書く）。cgroups の無い rootless の Docker では #283 の `--cpus` / `--memory` / `--pids-limit` が runc でも効かず、比べる基準にならないため
- イメージ: `python:3.12-slim`
- 引数: `SandboxManager` の docker モードと同じ `--network none --read-only --tmpfs /workspace:rw,exec,size=256m --tmpfs /tmp:rw,size=64m --env HOME=/workspace --cpus=1 --memory=512m --pids-limit=128 --cap-drop ALL --user nobody`
- 回数: 各 10 回の中央値
- 計測すること:
  - (a) `docker run --rm … python -c pass` の総時間
  - (b) `docker run --rm … python -c "import hashlib; [hashlib.sha256(str(i).encode()).hexdigest() for i in range(200000)]"` の処理時間（コンテナの中で測る）
  - `SandboxManager` の流れどおりの `docker run -d … sleep infinity`（作成）、`docker exec … python -c pass`（実行 1 回）、`docker rm -f`（破棄）の時間
  - #283 の引数のうち効かないもの（非 root、読み取り専用のルート、tmpfs での実行、capability、通信の遮断、メモリ・PID・CPU の上限）
- 実測しないもの: Docker Sandboxes（利用者の回答で一次資料の評価に変えた）、CubeSandbox（KVM の使えるホストが要る）

### gVisor と runc（#298）

2026-10-07 に計測した。**計測先のカーネルは 4.14 で、gVisor の要件（Linux 5.6 以上）を満たしていない**。計測を始めてから分かったことで、計測先のカーネルは選べなかった。runsc の値は要件の外での参考値として読む。下の「runsc で起きたこと」には、カーネルが原因の見込みのものがある（原因は未確認）。

計測先の構成:

| 項目 | 値 |
|---|---|
| CPU / メモリ | arm64（aarch64）8 vCPU / 16 GB |
| カーネル | 4.14（Amazon Linux 2） |
| Docker | 28.5.2、root で動く。cgroup v1（cgroupfs） |
| gVisor | release-20260928.0（`gvisor.tar.bz2` を公式の URL から取り、sha512 を確かめた） |
| `/dev/kvm` | 無い（`ls: cannot access '/dev/kvm': No such file or directory`） |

runsc で起きたこと:

- 既定の設定（`runsc install` のまま）では、どのコンテナも起動しなかった: `cannot create gofer process: creating gofer filestore files: failed to create filestore file ".gvisor.filestore.…" inside "/var/lib/docker/overlay2/…/merged": function not implemented`。ルートファイルシステムの上に置く filestore を作れない
- `--overlay2=root:memory`（filestore をメモリに置く）を付けて別名（`runsc-mem`）で登録すると起動した。下の runsc の値はこの設定のもの
- `docker exec` が失敗する: `waiting on pid 2: checking if sandbox is running: pidfd_open(…): function not implemented`。`pidfd_open` は Linux 5.3 で入ったシステムコールで、計測先のカーネル（4.14）には無い。`SandboxManager` はツールを `docker exec` で実行するので、この計測先では runsc を使えない
- `docker rm -f` が毎回約 10 秒かかった（runc は約 0.3 秒）。原因は未確認

所要時間（ミリ秒、10 回の中央値。括弧は最小〜最大）:

| 計測 | runc | runsc（`--overlay2=root:memory`） | 差 |
|---|---:|---:|---:|
| (a) `docker run --rm … python -c pass` | 734（629〜807） | 826（768〜949） | +92（+13%） |
| (b) ハッシュの処理（`docker run` で、コンテナの中で測る） | 176.4（172.9〜178.7） | 191.5（186.6〜198.2） | +15.1（+9%） |
| 作成 `docker run -d … sleep infinity` | 413（324〜513） | 525（445〜594） | +112（+27%） |
| 実行 `docker exec … python -c pass` | 48（48〜50） | 測れない（`pidfd_open`） | — |
| 破棄 `docker rm -f` | 339（294〜412） | 10319（10282〜10358） | +9980 |

#283 の引数が効くか（どちらのランタイムも `docker run --rm <引数> python:3.12-slim …` で確かめた）:

| 引数 | 確かめ方 | runc | runsc |
|---|---|---|---|
| `--user nobody` | `id -u` | 65534 | 65534 |
| `--read-only` | 誰でも書ける `/var/tmp`（モード 1777）に `touch` | `Read-only file system` | `Read-only file system` |
| `--tmpfs /workspace:…,exec` | 置いたスクリプトを実行 | 実行できた | 実行できた |
| `--cap-drop ALL` | `/proc/self/status` の `CapEff` | 0 | 0 |
| `--network none` | `1.1.1.1:443` へ TCP 接続 | `Network is unreachable` | `Network is unreachable` |
| `--memory=512m` | 700 MB を確保 | exit 137（OOM） | exit 137（OOM） |
| `--pids-limit=128` | スレッドを 400 本起動 | 127 本起動し、次で `RuntimeError` | 400 本とも起動した（効かない） |
| `--pids-limit=128` | `sleep` を 200 個起動 | 127 個起動し、次で `BlockingIOError`（`Resource temporarily unavailable`） | 51 個起動し、次で `OSError`（`Cannot allocate memory`）。プロセスの数ではなくメモリで止まった見込み |
| `--cpus=1` | 2 プロセスを 2 秒回し、子の CPU 時間 / 経過時間を見る（効けば約 2 秒、効かなければ約 4 秒） | 2.03 秒 / 2.05 秒 | 3.11 秒 / 2.12 秒（1 CPU 分を超えた。sandbox の中の CPU 時間の数え方が違う可能性もあり、効かないとは言い切れない） |

読み方:

- gVisor の上乗せは、この計測先で起動が 1 回あたり約 0.1 秒、Python の処理が約 1 割。どちらも `run_command` のタイムアウト（60 秒）に比べて小さい。ただし破棄（`docker rm -f`）は約 10 秒かかった（原因は未確認）。`SandboxManager` は TTL の掃除で破棄するので、要件どおりのカーネルでも遅いなら掃除の時間に効く
- 効かなかったのは `--pids-limit`（スレッドの数）。gVisor の公式の互換性の文書は「sandbox の中では cgroup の上限を強制しない」としている。`--cpus` は確かめきれていない
- 要件どおりのカーネル（5.6 以上）で、既定の設定での起動、`docker exec`、`docker rm -f` が runc と同じように動くかは未検証

再現コマンド: 下のスクリプトを、Docker が root で動く arm64 の Linux で、root で 1 回走らせる（`bash measure.sh`。回数は `N`、既定 10）。runsc を入れて Docker に登録し、dockerd に `SIGHUP` を送って読み直させる。ホストの Docker の設定を変えるので、使い捨てのマシンで走らせる前提で、片付けの手順は入れていない。値は 2026-10-07 の 5 回目の実行のもの（それまでの 4 回は、配布の URL の変更・既定の runsc の起動失敗・確かめ方の直しで走らせ直した）。

<details>
<summary>measure.sh</summary>

```bash
#!/bin/bash
# runc と gVisor（runsc）を SandboxManager の docker モードと同じ引数で比べる。root の Docker のある使い捨てのマシンで 1 回走らせる。
set -u
N=${N:-10}
IMG=python:3.12-slim
GVISOR=20260928.0
ARGS=(--network none --read-only --tmpfs /workspace:rw,exec,size=256m --tmpfs /tmp:rw,size=64m --env HOME=/workspace
      --cpus=1 --memory=512m --pids-limit=128 --cap-drop ALL --user nobody)
COMPUTE='import time,hashlib; t=time.perf_counter(); [hashlib.sha256(str(i).encode()).hexdigest() for i in range(200000)]; print(round((time.perf_counter()-t)*1000,1))'
ms() { echo $(( ($(date +%s%N) - $1) / 1000000 )); }
say() { echo "RESULT $*"; }
median() { python3 -c 'import sys,statistics as s; v=[float(x) for x in sys.argv[1:]]; print(s.median(v), min(v), max(v)) if v else print("NA")' "$@"; }

echo "== env"; uname -r; uname -m; nproc; free -m | head -2; docker version --format 'docker {{.Server.Version}}'
docker info --format 'cgroup {{.CgroupDriver}} v{{.CgroupVersion}}'
say "kvm $(ls -l /dev/kvm 2>&1)"

echo "== install runsc $GVISOR"
URL=https://storage.googleapis.com/gvisor/releases/release/$GVISOR/$(uname -m)
cd /tmp && curl -fsSLO $URL/gvisor.tar.bz2 -O $URL/gvisor.tar.bz2.sha512 && sha512sum -c gvisor.tar.bz2.sha512 \
  && tar -xjf gvisor.tar.bz2 -C /usr/local/bin || say "runsc_install FAILED"
runsc --version | head -1
runsc install
# 既定の runsc が filestore を作れない環境のために、filestore をメモリに置く版も登録する
runsc install --runtime runsc-mem -- --overlay2=root:memory
kill -HUP $(pidof dockerd); sleep 3          # dockerd は runtimes を SIGHUP で読み直す
say "runtimes $(docker info --format '{{range $k,$v := .Runtimes}}{{$k}} {{end}}')"

docker pull -q $IMG >/dev/null
for R in runsc runsc-mem; do
  docker run --rm --runtime=$R $IMG true 2>/tmp/err && say "$R start ok" || say "$R start FAILED: $(tail -1 /tmp/err)"
done

for R in runc runsc-mem; do
  echo "== $R"
  r() { docker run --rm --runtime=$R "${ARGS[@]}" $IMG "$@" 2>&1 | tail -1; }
  a=(); b=(); c=(); e=(); d=()
  for i in $(seq $N); do
    t=$(date +%s%N); docker run --rm --runtime=$R "${ARGS[@]}" $IMG python -c pass >/dev/null 2>&1 && a+=($(ms $t))
    v=$(docker run --rm --runtime=$R "${ARGS[@]}" $IMG python -c "$COMPUTE" 2>/dev/null) && b+=($v)
    n=iso-$R-$i
    t=$(date +%s%N); docker run -d --name $n --runtime=$R "${ARGS[@]}" $IMG sleep infinity >/dev/null 2>&1 && c+=($(ms $t))
    t=$(date +%s%N); docker exec -w /workspace $n timeout 60 python -c pass >/dev/null 2>/tmp/err && e+=($(ms $t)) \
      || { [ $i = 1 ] && say "$R exec FAILED: $(tail -1 /tmp/err)"; }
    t=$(date +%s%N); docker rm -f $n >/dev/null 2>&1 && d+=($(ms $t))
  done
  say "$R run_pass_ms $(median ${a[@]+"${a[@]}"}) n=${#a[@]}"
  say "$R compute_ms $(median ${b[@]+"${b[@]}"}) n=${#b[@]}"
  say "$R create_ms $(median ${c[@]+"${c[@]}"}) n=${#c[@]}"
  say "$R exec_pass_ms $(median ${e[@]+"${e[@]}"}) n=${#e[@]}"
  say "$R rm_ms $(median ${d[@]+"${d[@]}"}) n=${#d[@]}"

  # 引数が効くか（すべて docker run で、両方のランタイムに同じものを走らせる）
  say "$R user $(r id -u)"
  say "$R read_only $(r sh -c 'touch /var/tmp/x 2>&1 && echo WRITABLE')"
  say "$R tmpfs_exec $(r sh -c 'printf "#!/bin/sh\necho ran\n" > /workspace/s.sh && chmod +x /workspace/s.sh && /workspace/s.sh')"
  say "$R cap_eff $(r grep CapEff /proc/self/status)"
  say "$R network $(r python -c 'import socket; s=socket.socket(); s.settimeout(3)
try: s.connect(("1.1.1.1",443)); print("CONNECTED")
except Exception as ex: print("blocked:", ex)')"
  say "$R memory $(docker run --rm --runtime=$R "${ARGS[@]}" $IMG python -c 'b=bytearray(700*1024*1024); print("ALLOCATED")' >/dev/null 2>&1; echo "exit=$?")"
  say "$R threads $(r python -c 'import threading,time
n=0
try:
  for i in range(400): threading.Thread(target=time.sleep,args=(5,),daemon=True).start(); n+=1
  print("started", n)
except Exception as ex: print("started", n, "then", type(ex).__name__)')"
  say "$R processes $(r python -c 'import subprocess
ps=[]
try:
  for i in range(200): ps.append(subprocess.Popen(["sleep","5"]))
  print("started", len(ps))
except Exception as ex: print("started", len(ps), "then", type(ex).__name__, ex)')"
  say "$R cpus $(r python -c 'import os,time,multiprocessing as m
def burn():
  t=time.time()
  while time.time()-t<2: pass
ps=[m.Process(target=burn) for _ in range(2)]; w=time.time(); [p.start() for p in ps]; [p.join() for p in ps]
c=os.times(); print("cpu_s=%.2f wall_s=%.2f" % (c.children_user+c.children_system, time.time()-w))')"
done
echo "== done"
```

</details>

### Kata Containers（#298）

未検証。計測先に `/dev/kvm` が無い（`ls -l /dev/kvm` → `ls: cannot access '/dev/kvm': No such file or directory`）。Kata は「nested virtualization か bare metal が要る」（1 節の Kata の行の install）ので、この計測先では動かせない。KVM の使えるホストで測るかは #300 の判断に回す。

## 5. 推す案

（#300 で書く）
