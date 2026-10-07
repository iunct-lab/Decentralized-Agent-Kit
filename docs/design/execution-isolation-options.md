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

（#299 で書く）

## 4. 実測

### 共通の条件

- 計測先: Docker が root で動き cgroups の使える、使い捨ての arm64 の Linux（構成は下の表に書く）。cgroups の無い rootless の Docker では #283 の `--cpus` / `--memory` / `--pids-limit` が runc でも効かず、比べる基準にならないため
- イメージ: `python:3.12-slim`
- 引数: `SandboxManager` の docker モードと同じ `--network none --read-only --tmpfs /workspace:rw,exec,size=256m --tmpfs /tmp:rw,size=64m --env HOME=/workspace --cpus=1 --memory=512m --pids-limit=128 --cap-drop ALL --user nobody`
- 回数: 各 10 回の中央値
- 計測すること:
  - (a) `docker run --rm … python -c pass` の総時間
  - (b) 作成済みのコンテナで `python -c "import hashlib; [hashlib.sha256(str(i).encode()).hexdigest() for i in range(200000)]"` の処理時間（コンテナの中で測る）
  - `SandboxManager` の流れどおりの `docker run -d … sleep infinity`（作成）、`docker exec … python -c pass`（実行 1 回）、`docker rm -f`（破棄）の時間
  - #283 の引数のうち効かないもの（非 root、読み取り専用のルート、tmpfs での実行、capability、通信の遮断、メモリ・PID・CPU の上限）
- 実測しないもの: Docker Sandboxes（利用者の回答で一次資料の評価に変えた）、CubeSandbox（KVM の使えるホストが要る）

### gVisor と runc（#298）

計測先の構成:

| 項目 | 値 |
|---|---|

所要時間（ミリ秒、10 回の中央値）:

| 計測 | runc | runsc | 差 |
|---|---:|---:|---:|
| (a) `docker run --rm … python -c pass` | | | |
| (b) ハッシュの処理（コンテナの中で測る） | | | |
| 作成 `docker run -d … sleep infinity` | | | |
| 実行 `docker exec … python -c pass` | | | |
| 破棄 `docker rm -f` | | | |

#283 の引数が効くか:

| 引数 | runc | runsc |
|---|---|---|

再現コマンド:（#298 で書く）

### Kata Containers（#298）

（#298 で書く。計測先の `ls -l /dev/kvm` の結果と、計測の表または未検証の記録）

## 5. 推す案

（#300 で書く）
