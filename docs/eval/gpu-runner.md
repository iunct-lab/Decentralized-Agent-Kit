# 夜間の実LLM評価を GPU サーバで回すかの材料

夜間評価（`.github/workflows/nightly-eval.yml`）は無料ランナー（CPU のみ）の Ollama `llama3.1:8b` で回っている。
CPU 推論が遅いので `DAK_AGENT_RUN_TIMEOUT=600`、スモークの `timeout-minutes: 45` まで緩めてある。
これを手元の Linux GPU サーバで回すときの、目的・方式・脅威と対策・空き確認・運用を比べる。
元要望は #69、計画は PBI #339。選ぶのは利用者で、選んだ結果はこの文書の「選択」と PBI #339 の決定ログに残す。

## 今の設定（2026-10-02 確認）

| 項目 | 値 | 見た場所 |
|---|---|---|
| リポジトリの公開範囲 | public（Organization の所有） | `gh api repos/iunct-lab/Decentralized-Agent-Kit` |
| 登録済みの self-hosted runner | 0 台 | `gh api repos/iunct-lab/Decentralized-Agent-Kit/actions/runners` |
| fork の PR のワークフローの承認 | `first_time_contributors`（初めての貢献者だけ承認が要る） | `gh api repos/iunct-lab/Decentralized-Agent-Kit/actions/permissions/fork-pr-contributor-approval` |
| Organization の runner group | 読めなかった（手元のトークンでは 403） | `gh api orgs/iunct-lab/actions/runner-groups` |

## 1. 目的の優先順位

#69 が挙げた動機は 3 つ。どれを先にするかで、モデルと方式の選び方が変わる。

| 目的 | 得られるもの | 決めること |
|---|---|---|
| A. 時間短縮 | 45 分の上限に近い CPU 推論を、数分にする | 今と同じ `llama3.1:8b` を GPU で回す。`history.jsonl` 上は今と同じ系列で、所要時間だけが比べられる |
| B. 大きいモデル | 8B より大きいモデルでの pass_rate | モデル名が変わるので、`history.jsonl` 上は別の系列になる（今の系列と pass_rate を直接比べない） |
| C. llama.cpp 経路 | `docker-compose.llamacpp.yml` の経路（`scripts/smoke_llamacpp.sh`）が毎晩回る | 推論サーバを llama-server にする。Ollama の経路は無料ランナーの評価に残る |

推奨: **A を先に、C を同時に**。A は今の系列と比べられるので GPU 化の効果がそのまま見える。
推論サーバを llama-server にすれば C も同じ実行で満たせる。B はモデルを足すだけなので、A が安定してから。

## 2. 方式の比較

- (a) GPU サーバを self-hosted runner にする。ジョブは `runs-on: [self-hosted, linux, gpu]` で、runner が Docker のスタックと推論サーバを直接使う
- (b) runner にしない。無料ランナーのジョブが、GPU サーバの推論 API（llama-server の `/v1` か Ollama）にだけ届く経路を張る。
  スタック（agent・mcp-server など。どれも CPU で足りる）は今どおり無料ランナーで動き、推論だけが GPU サーバに行く。
  `docs/guides/local_llm_llamacpp.md` の「動かすのはトンネルであってコードではない」に合う

| 観点 | (a) self-hosted runner | (b) 推論 API へのトンネル |
|---|---|---|
| GPU サーバで他人のコードが動く可能性 | ある。ワークフローの定義は PR 側のものが使われるので、`runs-on: [self-hosted, gpu]` を書いた PR を出せる。承認の設定と人の目だけが止める | 無い。GPU サーバで動くのは推論サーバだけ。届くのは推論のリクエスト（プロンプト）だけ |
| 漏れたときの影響 | runner のユーザーの権限すべて。Docker を使えるなら root 相当（下の脅威の表） | 推論サーバの HTTP API 全体を他人に使われる。GPU の時間を取られるほか、Ollama ならモデルの削除・pull の API（`/api/delete` など）にも届く。サーバの他のポートとホストには届かない（ACL で推論ポートだけを許す） |
| 要る secret | なし（`GITHUB_TOKEN` だけ） | Tailscale の認証。推奨は workload identity federation で、置くのは `TS_OAUTH_CLIENT_ID` と `TS_AUDIENCE`（長期の秘密ではない）。OAuth client なら `TS_OAUTH_SECRET` も |
| 利用者の設定作業 | runner の登録（専用ユーザー・使い捨て）、Docker（rootless か VM）、推論サーバの常駐、fork の PR の承認設定、30 日以内の runner の更新 | GPU サーバに Tailscale を入れる、tailnet の ACL（runner のタグから推論ポートだけ）、federated identity の作成、推論サーバの常駐 |
| 速度 | 推論もスタックも手元。CPU ランナーの起動と Ollama の pull が無くなる | 推論は GPU。スタックの起動は今どおり無料ランナー（数分）。Ollama の install と pull（約 4.9GB）は無くなる |
| GPU サーバが使えない夜 | ジョブが runner を待ち続ける（24 時間で失敗）。無料ランナーへの切り替えは別の定期実行にする（PBI #339 の決定ログの設計） | 同じジョブの中で `/health` に落ちたら、今の CPU の Ollama に切り替えられる。定期実行は 1 つのまま |
| 空き確認 | runner の上で `nvidia-smi`・`uptime` を直接読める | 推論サーバの `/health` しか見えない。`nvidia-smi` の空きを見るなら、GPU サーバ側で推論サーバの起動を止める運用にする |
| 費用 | なし | Tailscale の Personal プランは無料で、6 ユーザーまで、ephemeral のノードは月 1,000 分まで。**非商用に限る**。毎晩 30 分なら月 900 分前後で枠に収まるが、余裕は少ない。この Organization の用途が非商用に当たるかは利用者が判断する |
| 今の Task との関係 | #342・#343 をそのまま進める | #341 はそのまま使う。#342・#343 を書き直す |

推奨: **(b)**。公開リポジトリで (a) を安全にするには、下の表の対策をすべて揃える必要があり、それでも承認の設定ひとつを間違えると GPU サーバ上で他人のコードが動く。
(b) は漏れても推論を使われるだけで、目的 A・C の速度はほぼ同じに得られる。(b) の弱みは Tailscale という外部サービスへの依存と、無料枠の非商用の条件。
Cloudflare Tunnel など他の製品は確かめていない（未確認）。

## 3. 脅威と対策（方式 (a) の場合）

| 脅威 | 対策 | 誰が |
|---|---|---|
| fork の PR が `runs-on: [self-hosted, gpu]` を書き、GPU サーバで任意のコードを動かす | fork の PR の承認を「Require approval for all external contributors」にする（今は `first_time_contributors`）。承認の前に差分のワークフローを読む | 利用者（リポジトリの設定） |
| 同上（正直な設定ミス。メンバーが PR のワークフローに self-hosted を書く） | #341 のチェック（`dak-maint workflow-guard`）が CI で落とす。**悪意のある PR は自分の変更でこのチェックも外せるので、悪意には効かない** | #341 |
| `pull_request_target` のワークフロー | 承認の設定に関係なく必ず動く。self-hosted を使わせない（#341 のチェックの対象） | #341 |
| 前のジョブの残骸・持続的な侵害（バックドアを置かれる） | runner を `--ephemeral` で登録し、1 ジョブで撤去される使い捨てにする。同じ機械を使い回すなら、ジョブごとに作り直す VM かコンテナの中で動かす | 利用者（runner の登録） |
| Docker の権限が root 相当（`docker` グループは root 相当の権限になる） | runner を専用ユーザーで動かし、rootless Docker を使う。スタックは GPU を使わない（推論サーバはホストで常駐）ので、rootless でも GPU の対応は要らない。もっと強く分けるなら VM に閉じ込める | 利用者（GPU サーバ） |
| secret が読まれる | GPU のジョブには `GITHUB_TOKEN`（`contents: write` だけ）以外を渡さない。同じ runner で別のジョブの引数が `ps` で見えるので、secret を引数で渡さない | ワークフロー（#342） |
| runner を他のリポジトリから使われる | Organization の runner group で、このリポジトリだけに使わせる。group の既定では public リポジトリは使えないので、明示して許す。追加の group は GitHub Team プランから。この Organization のプランと group は読めなかった（未確認） | 利用者（Organization の設定） |
| runner の版が古いまま | 新しい版が出てから 30 日以内に更新しないと、ジョブが割り当てられなくなる。自動更新を止めない | 利用者（運用） |

方式 (b) の場合の対策は 3 つ: tailnet の ACL で runner のタグから推論ポートだけを許す、federated identity を
このリポジトリの `schedule` / `workflow_dispatch` の実行だけが使えるように絞る（fork の PR には `id-token: write` が渡らない）、
推論以外の API を出さない。ACL はポートまでしか絞れないので、Ollama（モデルの削除・pull を受ける）は使わず llama-server にし、
管理系を開ける起動オプション（`--props` など）を付けない。それでも推論以外の API（`/slots` など）は残るので、もっと絞るなら
推論のパス（`/v1/chat/completions`・`/health`）だけを通すリバースプロキシを前に置く。
#341 のチェックはどちらの方式でも入れる。

## 4. 空き確認の条件

GPU サーバは共有マシンで、使う前に空きを確かめる運用がある（#69）。評価の前に次を見て、1 つでも落ちたら記録せずに終わり、無料ランナーの評価に任せる。

| 確認 | コマンド | 閾値（案） |
|---|---|---|
| GPU の空きメモリ | `nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits` | 8192 MiB 以上（`llama3.1:8b` の Q4 は約 4.9GB。KV キャッシュの余裕を足す）。推論サーバがモデルを常駐させているなら、モデルの分はもう引かれているので 2048 MiB 以上（KV キャッシュと生成の余裕） |
| ホストの負荷 | `uptime` の 1 分平均を `nproc` で割った値 | 0.5 未満 |
| 推論サーバ | llama-server は `curl -sf http://<推論サーバ>/health`（読み込み中は 503）、Ollama は `/api/version` | 200 が返る |

方式 (b) では見られるのは 3 行目だけ。共有の都合で使ってほしくない夜は、GPU サーバ側で推論サーバを止める（止めると `/health` に落ちて CPU に切り替わる）。

## 5. モデルの案

| 案 | 中身 | `history.jsonl` |
|---|---|---|
| 1 | 今と同じ `llama3.1:8b` を GPU で回す | 同じ系列。`runner`（`gpu` / `github-hosted`）と所要時間 `duration_sec` で比べる |
| 2 | 大きいモデル（例: 30B 前後の Q4）に変える | 別のモデル名の系列になる。今の系列とは pass_rate を直接比べない |

推奨: 案 1 から（目的 A）。

## 6. 運用（骨組み。仕上げは #343）

### 変数

- `DAK_GPU_EVAL_ENABLED`（リポジトリ変数）: `true` のときだけ GPU で評価する。未設定・`false` なら今の無料ランナーの評価だけが動く。
  一時停止はこれを `false` にする（`gh variable set DAK_GPU_EVAL_ENABLED --body false`）

### 登録

（#343 で書く。方式 (a) なら専用ユーザー・`--ephemeral`・ラベル `gpu`、方式 (b) なら Tailscale のタグと ACL）

### 更新

（#343 で書く。runner は新しい版が出てから 30 日以内）

### 一時停止

`DAK_GPU_EVAL_ENABLED` を `false` にする。GPU サーバの作業で一晩だけ止めるなら、推論サーバを止めれば空き確認に落ちて無料ランナーに切り替わる。

### 撤去

（#343 で書く。runner の登録解除か Tailscale のノードの削除、変数の削除）

### 空き確認に落ちた夜の見え方

（#343 で書く。step summary の理由と、無料ランナーの行）

## 7. 選択（利用者が決める）

1. 目的の優先順位（A / B / C）
2. 方式（(a) / (b)）
3. 対策: (a) なら承認の設定・使い捨て・rootless か VM・runner group、(b) なら Tailscale の無料枠の条件を満たすか
4. 空き確認の閾値とモデル（案 1 / 案 2）

（回答が来たらここに書く）

## 出典（確認日 2026-10-02）

- GitHub, [Secure use reference](https://docs.github.com/en/actions/reference/security/secure-use):
  "Self-hosted runners should almost never be used for public repositories on GitHub, because any user can open pull requests against the repository and compromise the environment."
  fork から PR を開ける人は self-hosted runner の環境・secret・`GITHUB_TOKEN` に届きうる。JIT runner は 1 ジョブで撤去される
- GitHub, [Managing GitHub Actions settings for a repository](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/enabling-features-for-your-repository/managing-github-actions-settings-for-a-repository):
  承認の選択肢 3 つ（"Require approval for first-time contributors who are new to GitHub" / "Require approval for first-time contributors" / "Require approval for all external contributors"）。`pull_request_target` は承認の設定に関係なく動く
- GitHub, [Self-hosted runners reference](https://docs.github.com/en/actions/reference/runners/self-hosted-runners):
  "If the job remains queued for more than 24 hours, the job will fail."、30 日以内に更新しないとジョブが割り当てられない、`--ephemeral`
- GitHub, [Managing access to self-hosted runners](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/manage-access):
  追加の runner group は Team プランから。"By default, only private repositories can access runners in a runner group, but you can override this."
- Docker, [Linux post-installation steps](https://docs.docker.com/engine/install/linux-postinstall/): "The `docker` group grants root-level privileges to the user."
- Tailscale, [Tailscale GitHub Action](https://tailscale.com/kb/1276/tailscale-github-action): workload identity federation を推奨（`TS_OAUTH_CLIENT_ID`・`TS_AUDIENCE`、`id-token: write`）、ノードは ephemeral、タグが要る、`tailscale/github-action@v4`
- Tailscale, [Pricing](https://tailscale.com/pricing): Personal は無料・6 ユーザーまで・ephemeral は月 1,000 分・非商用に限る
