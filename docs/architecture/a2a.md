# A2A の窓口: 仕様 1.0 とのずれと版の方針

PBI #309 / Task #310。DAK の A2A 窓口を、公式の適合テスト a2a-tck と公式 SDK `a2a-sdk` のクライアントで試し、
A2A 仕様 1.0 とのずれを測った結果と、版の混在（0.3 の相手を受け続けるか）の方針をまとめる。
この文書は調べた結果と判断までを扱い、コードは変えない（直すのは #311・#312・#313）。

- §1 窓口の場所と今の版
- §2 仕様 1.0 とのずれ
- §3 a2a-tck の結果
- §4 版の混在の方針
- §5 Agent Communication Protocol を別に採用しない
- §6 ほかのフレームワークのクライアント（候補）
- §7 別の A2A エージェントに任せる（Consumer の `a2a_peers`）
- §8 適合テスト（CI の a2a-tck）

調べたもの（2026-10-07）:

- 仕様: [A2A specification](https://a2a-protocol.org/latest/specification/)（v1.0、2026-03-12 公開。[告知](https://a2a-protocol.org/latest/announcing-1.0/)）。
  Agent Card のフィールドは a2a-tck が同梱する `specification/a2a.proto` の `AgentCard` / `AgentInterface` / `AgentCapabilities` と照合した
- 公式 Python SDK `a2a-sdk`: サーバ側は今の main の 0.3.26 と、書き捨ての枝で上げた 1.2.2（`>=1.1,<2` で解決した版）。
  クライアントは 1.2.2（1.0 の JSON-RPC）と 0.3.26（古い DAK が使う版）
- 適合テスト: [a2a-tck](https://github.com/a2aproject/a2a-tck) のコミット `263b9cfaf16a554bdfb166a7ba5b67716e946349`
- `a2a-sdk` の移行ガイド [`docs/migrations/v1_0/README.md`](https://github.com/a2aproject/a2a-python/blob/main/docs/migrations/v1_0/README.md) の「6. Supporting v0.3 Clients」（0.3 のクライアントも受ける手順）
- DAK 側: google-adk 2.11.0 の `google/adk/cli/fast_api.py`（`a2a=True` の組み立て）と `google/adk/a2a/_compat.py`、
  `agent/entrypoint.sh`、`agent/dak_agent/a2a_peer_manager.py`
- 構成: fake-LLM の構成（`docker-compose.yml` + `docker-compose.test.yml` の `agent` と `fake-llm`）。窓口は `http://localhost:8000/a2a/dak_agent`

## 1. 窓口の場所と今の版

| 項目 | 場所 |
|---|---|
| Agent Card | `GET /a2a/dak_agent/.well-known/agent-card.json`（`agent/entrypoint.sh` が起動時に `agent/dak_agent/agent.json` を書き、ADK がそれを読んで配る） |
| JSON-RPC | `POST /a2a/dak_agent` |
| 組み立て | `agent/dak_agent/server.py` の `get_fast_api_app(..., a2a=True)`。ADK が `agents_dir` の下で `agent.json` のあるディレクトリごとに窓口を足す（`_compat.attach_a2a_routes_to_app`、`prefix=/a2a/<ディレクトリ名>`）。パスはディレクトリ名 `dak_agent` で決まり、`AGENT_NAME` はカードの `name` を変えるだけ |

今の版: `a2a-sdk` 1.x（`agent/pyproject.toml` は `a2a-sdk>=1.1,<2`。#311）。カードは 1.0 の形で、`supportedInterfaces` に同じ JSON-RPC の URL を
`protocolVersion` 1.0 と 0.3 の 2 つの窓口として書く（§4）。
調べた時点（#310）は `a2a-sdk` 0.3.26（`a2a-sdk>=0.2.0,<1` にピンし、`.github/dependabot.yml` は major を止めていた）で、
カードは 0.3 の形（`"protocolVersion": "0.2.6"`）だった。§2・§3 の「今」と「今の main」はこの時点のこと。

google-adk 2.11 は `a2a-sdk` 0.3 と 1.x の両方を扱う（`_compat.py` が `IS_A2A_V1` で分ける）。
1.x のときは JSON-RPC の窓口を `enable_v0_3_compat=True`（既定）で作るので、同じ URL で 0.3 の `message/send` も受ける。

## 2. 仕様 1.0 とのずれ

「今」は今の main で確かめた事実。「直す Task」の列が、そのずれを直す Task。

| 項目 | 今 | 1.0 | 直す Task |
|---|---|---|---|
| カードの形 | トップレベルの `url` と `preferredTransport`。`supportedInterfaces` が無い | `supportedInterfaces`（`url`・`protocolBinding`・`protocolVersion` は必須）。トップレベルの `url` は無い | #311 |
| 版の宣言 | `"protocolVersion": "0.2.6"`（カード全体に 1 つ） | 窓口ごとの `protocolVersion: "1.0"` | #311 |
| 拡張カード | トップレベルの `supportsAuthenticatedExtendedCard: false` | `capabilities.extendedAgentCard` | #311（書かない。既定が false） |
| メソッド名 | 0.3 の `message/send` / `message/stream` だけ。`SendMessage` は `-32601 Method not found` | `SendMessage`・`SendStreamingMessage`・`GetTask` など（PascalCase） | #311（`a2a-sdk` 1.x に上げる） |
| ストリーミングの宣言 | `"capabilities": {}`（宣言なし） | `capabilities.streaming: true` を宣言すると、相手は `SendStreamingMessage` を使う | #311（1.x で `SendStreamingMessage` の往復を確かめた。§3） |
| 公式 SDK 1.x のクライアント | カードを読んだ後に `ValueError: no compatible transports found.` で止まる（非ストリーミング・ストリーミングとも） | カードの `supportedInterfaces` から窓口を選ぶ | #311 |
| 版の混在 | 0.3 のクライアント（`a2a-sdk` 0.3.26）はカードを読んで往復できる | — | #311（§4 の方針で、1.x に上げても受け続ける） |
| Consumer のカードの場所 | `a2a_peer_manager.py` の `create_remote_a2a_agents` が `peer.url + "/a2a/dak_agent/.well-known/agent-card.json"` と決め打ち。相手の窓口が `/a2a/dak_agent` でない（DAK 以外の A2A エージェント、窓口の URL を `url` に書いた設定など）とつながらない | カードの URL は相手が決める（既定は窓口の下の `/.well-known/agent-card.json`） | #312 |
| 適合テスト | 回帰テストは `tests/integration/test_a2a.py` だけで、0.3 の形を固定している | a2a-tck の MUST 段階 | #313 |
| 文書 | `docs/architecture/overview.md` と `agent/README.md` が、今は無い `/task/send` を窓口として書いている | — | #311 |
| メタデータのキー | ADK が Task・イベントの `metadata` に `adk_session_id` などの snake_case のキーを入れる（DAK の状態の `dak_compaction_count` なども `adk_actions` の中に出る） | a2a-tck の DM-SERIAL-001 は JSON のフィールド名を camelCase に限る（§3） | #313 で扱いを決める（ADK の変換が付けるキーで、DAK の中では直せない） |

## 3. a2a-tck の結果

実行したコマンド（MUST 段階、JSON-RPC だけ）:

```bash
git clone https://github.com/a2aproject/a2a-tck.git && cd a2a-tck
git checkout 263b9cfaf16a554bdfb166a7ba5b67716e946349
uv venv && uv pip install -e .
uv run ./run_tck.py --sut-host http://localhost:8000/a2a/dak_agent --transport jsonrpc --level must
```

a2a-tck は `{sut-host}/.well-known/agent-card.json` を読み、カードの `supportedInterfaces` から試す窓口を決める。
pytest の出力（`passed` / `failed` / `errors`）の数はテストの数、それ以外（表と本文）の件数は要件の数。`must_compatibility` は tck の集計（`reports/compatibility.json` の `summary`）のままで、MUST の要件のうち PASS の割合（SKIPPED を分母から除く）。

### 3.1 今の main（`a2a-sdk` 0.3.26、0.3 の形のカード）

```
E   Failed: Agent card declares no supportedInterfaces — cannot determine which transports to test
3 failed, 7 passed, 30 deselected, 225 errors in 1.84s
"must_compatibility": "5.3%"
```

| 要件 | 結果 | 理由 |
|---|---|---|
| CARD-DISC-001 | PASS | カードは取れる |
| CARD-PROTO-002、JSONRPC-SVC-001、HTTP_JSON-URL-001、HTTP_JSON-URL-002、HTTP_JSON-QP-001 | PASS | SUT に問い合わせない静的な検査（tck が持つ仕様の定義を確かめる） |
| CARD-STRUCT-001 | FAIL | `Missing required fields: {'supportedInterfaces'}` |
| CARD-PROTO-001 | FAIL | `supportedInterfaces must be a list` |
| BIND-FIELD-001 | FAIL | `At least one protocol binding must be declared` |
| それ以外の 105 件（CORE-*、DM-*、STREAM-*、JSONRPC-* など） | NOT TESTED | カードに窓口が無く、クライアントを作れない（225 件の error） |

### 3.2 書き捨ての枝（`a2a-sdk` 1.2.2、1.0 の形のカード）

`agent/pyproject.toml` を `a2a-sdk>=1.1,<2` にして lock を取り直し、`agent/entrypoint.sh` のカードを次の形にした（`capabilities.streaming: true`）。
`supportedInterfaces` に 1.0 の窓口だけを書いた場合と、同じ URL の 0.3 の窓口も並べた場合（§4）の 2 通りを試し、a2a-tck の結果は同じだった。

```json
"supportedInterfaces": [
  {"url": "${AGENT_PUBLIC_URL}", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"},
  {"url": "${AGENT_PUBLIC_URL}", "protocolBinding": "JSONRPC", "protocolVersion": "0.3"}
]
```

```
6 failed, 66 passed, 163 skipped, 30 deselected in 3.03s
"must_compatibility": "69.1%"
```

MUST の要件 114 件のうち、PASS 56 件・FAIL 3 件・SKIPPED 33 件・NOT TESTED 22 件（SKIPPED と NOT TESTED は、gRPC・HTTP+JSON の窓口、push 通知、拡張カードなど、カードが宣言していない機能）。FAIL の 3 件:

| 要件 | 落ちたテスト | 理由 | DAK で直せるか |
|---|---|---|---|
| DM-ART-001 | `test_artifacts.py` の `TestTextArtifact` / `TestFileArtifact` / `TestFileUrlArtifact` / `TestDataArtifact`（4 件） | 「SUT に特定の種類の artifact を出させるメッセージ」を送り、決まった中身（`Generated text content`、`output.txt` のファイルなど）を期待する。a2a-tck のシナリオ（`scenarios/*.feature`）から生成した SUT を前提にしたテスト | 直せない（DAK の返事は LLM が決める） |
| DM-MSG-001 | `test_artifacts.py` の `TestMessageResponse::test_returns_message_with_text_part` | `Expected a Message response, but got a Task`。ADK の窓口はいつも Task で返す | 直せない（ADK の実装） |
| DM-SERIAL-001 | `test_data_model.py` の JSON のフィールド名の検査 | `Found snake_case field names: {'adk_session_id', 'adk_actions', 'dak_compaction_count', …}`。ADK が `metadata` に入れるキー | DAK の中では直せない（ADK の `event_converter` が付ける） |

### 3.3 公式 SDK のクライアント

| クライアント | 今の main | 1.x（1.0 だけのカード） | 1.x（1.0 + 0.3 のカード） |
|---|---|---|---|
| `a2a-sdk` 1.2.2、`streaming=False`（`SendMessage`） | `no compatible transports found` | 往復できた（Task `TASK_STATE_COMPLETED`、artifact に返事の文） | 往復できた |
| `a2a-sdk` 1.2.2、`streaming=True`（`SendStreamingMessage`） | `no compatible transports found` | 往復できた（`task` → `status_update`（WORKING）→ `artifact_update` → `status_update`（COMPLETED）） | 往復できた |
| 生の JSON-RPC `SendMessage`（`A2A-Version: 1.0`） | `-32601 Method not found` | 往復できた | 往復できた |
| `a2a-sdk` 0.3.26（古い DAK と同じ版）がカードを読む | 読めた | **読めない**（`url: Field required`） | 読めた（トップレベルに `url`・`preferredTransport`・`protocolVersion: "0.3"` が足されて配られる） |
| `a2a-sdk` 0.3.26 で送る（`message/send` / `message/stream`） | 往復できた | （カードで止まる） | 往復できた |
| 生の JSON-RPC `message/send`（ヘッダなし） | 往復できた | 往復できた | 往復できた |

## 4. 版の混在の方針

**0.3 の相手も受け続ける**。`a2a-sdk` を 1.x に上げ、カードの `supportedInterfaces` に、同じ JSON-RPC の URL を 1.0 と 0.3 の 2 つの窓口として書く（#311）。

根拠:

- 窓口（JSON-RPC）は追加の作業なしで両方を受ける。google-adk 2.11 は `a2a-sdk` 1.x の JSON-RPC の窓口を `enable_v0_3_compat=True` で作るので、0.3 の `message/send` も 1.0 の `SendMessage` も同じ URL で通る（§3.3）
- カードは、0.3 の窓口を並べると両方の版の相手が読める。`a2a-sdk` 1.x はカードを配るときに、`supportedInterfaces` に 0.3 の版の窓口があればそれを 0.3 の形（トップレベルの `url` など）にも書き足す（`a2a/server/request_handlers/response_helpers.py` の `agent_card_to_dict`）。1.0 の窓口だけだと、0.3 の相手（`a2a-sdk` 0.3 を使う古い DAK を含む）はカードを読めない
- 1.0 の相手と a2a-tck は 1.0 の窓口を選び、結果は 1.0 だけのカードと変わらない（§3.2）
- 0.3 の相手を切り捨てる理由が無い。混在している間も、古い DAK から新しい DAK への委譲は切れない

逆向き（新しい DAK が古い DAK に任せる）は切れる。新しい DAK の Consumer（`RemoteA2aAgent`、`a2a-sdk` 1.x のクライアント）は、
古い DAK のカード（`protocolVersion: "0.2.6"`）を 0.3 より前の版の窓口として読み、使える窓口が無い（`no compatible transports found`。§3.3 の今の main の列と同じ）。
相手の DAK を上げると直る。DAK 同士で混在させるときは、呼ばれる側から先に上げる。

この形は `a2a-sdk` の移行ガイドの「6. Supporting v0.3 Clients」の 2 つの手順（`supported_interfaces` に 0.3 の `AgentInterface` を足す、route に `enable_v0_3_compat=True`）と同じで、後者は ADK が済ませている。互換の範囲（カードは 1.0 と 0.3 の項目の和で配る、など）は移行ガイドが参照する取りまとめの Issue [a2a-python#742](https://github.com/a2aproject/a2a-python/issues/742)（closed）にある。

受けるのをやめるのは、0.3 の窓口を宣言から消すとき（`a2a-sdk` が 0.3 の互換を外したとき、または相手が 1.0 にそろったとき）。そのときはこの節を書き換える。

## 5. Agent Communication Protocol を別に採用しない

元要望 #53 は、IBM / BeeAI の Agent Communication Protocol（ACP）の窓口を DAK に足す案だった。採用しない。

- ACP は 2025-08-29 に、Linux Foundation の LF AI & Data の下で A2A に合流すると発表された。ACP のチームは開発を止め、技術を A2A に持ち込む。
  BeeAI のプラットフォームも A2A を使うようになり、BeeAI のエージェントは `A2AServer` のアダプタで A2A に対応し、外の A2A のエージェントは `A2AAgent` のクライアントで呼べる。ACP から A2A への移行ガイドもある
  （出典: [ACP Joins Forces with A2A Under the Linux Foundation's LF AI & Data](https://lfaidata.foundation/communityblog/2025/08/29/acp-joins-forces-with-a2a-under-the-linux-foundations-lf-ai-data/)、2025-08-29、2026-10-07 に確認）
- したがって、DAK が A2A 1.0 に沿っていれば、BeeAI を含む ACP の側のエージェントとも A2A でつながる。別の窓口を持つ価値は無い

同じ「ACP」でも、エディタとつなぐ Agent Client Protocol（`docs/architecture/acp_adapter.md`）とは別のもの。

## 6. ほかのフレームワークのクライアント（候補）

この PBI では公式 Python SDK と a2a-tck で代表させ、ほかは試していない。試すときの候補（名前だけ）:

- BeeAI Framework の `A2AAgent`
- 公式 SDK の JavaScript（`@a2a-js/sdk`）・Java（`a2a-java`）・Go（`a2a-go`）
- LangGraph の A2A 対応
- google-adk の `RemoteA2aAgent`（DAK の Consumer が使う。#312 で DAK 同士の委譲として確かめる）

## 7. 別の A2A エージェントに任せる（Consumer の `a2a_peers`）

`ENABLE_A2A_CONSUMER=true` の DAK は、`agent_config.yaml` の `a2a_peers` に書いた相手を ADK の `RemoteA2aAgent`（sub-agent）にし、
モデルが `transfer_to_agent` で任せる（`agent/dak_agent/a2a_peer_manager.py`）。相手のカードの場所は次の順で決める（`resolve_agent_card_url`）:

| 書き方 | カードの場所 |
|---|---|
| `card_url: <URL>` | その URL |
| `url:` がカードの URL（`…/.well-known/agent-card.json` で終わる） | そのまま |
| `url:` が相手の A2A 窓口（例 `https://peer.example/a2a/dak_agent`） | 窓口の下の `/.well-known/agent-card.json` |
| `url:` にパスが無い（例 `https://peer.example`。前の書き方） | DAK の既定の窓口 `/a2a/dak_agent` を補う。新しい書き方を示す警告をログに出す。DAK でない相手がホストの直下（`/.well-known/agent-card.json`）でカードを配るときは `card_url` を書く |

```yaml
a2a_peers:
  - name: "dak_peer"                              # sub-agent の名前（transfer_to_agent で使う）
    url: "https://peer.example/a2a/dak_agent"     # 相手の A2A 窓口。カードはその下（https。下の段落）
    capabilities: ["..."]
    card_public_key_file: "/run/secrets/peer-card.pub.pem"  # 相手のカードの署名を確かめる公開鍵（下の「カードの署名と検証」）。書かなければ unchecked
```

ADK（2.11）の `RemoteA2aAgent` は、ネットワークから取るカードの URL と、カードが示す RPC の URL の両方に **https** を求める（http はループバックの名前 `localhost`・`*.localhost`・`127.0.0.1` などだけ。外す設定は無い）。
カードの RPC の URL は、カードを取った URL と同じ origin でなければならない。別のホストの DAK に任せるときは、相手の窓口を https で公開する。
`url` にクエリ文字列を付けない（カードのパスがクエリの後ろに付き、カードを取れない）。

DAK の窓口のパスは、相手の `AGENT_NAME` ではなく、ADK のアプリ（エージェントのディレクトリ）の名前 `dak_agent` で決まる。`AGENT_NAME` はカードの `name` を変えるだけ。
fake-LLM の構成の `agent-consumer` → `agent-peer`（カードの名前は `dak_peer`。http で呼ぶため、compose のネットワークの別名 `agent-peer.localhost` を使う）で、この委譲を `tests/integration/test_a2a.py::test_delegation_to_peer_with_non_default_name` が確かめる。

### カードの署名と検証

PBI #638。A2A 1.0 の署名付き Agent Card（仕様 §8.4。採否は [agent_discovery.md](agent_discovery.md) §6、PBI #318）。

**自分のカードに署名する**（既定は署名しない）:

| 環境変数 | 意味 |
|---|---|
| `AGENT_CARD_SIGNING_KEY_FILE` | 署名の秘密鍵の PEM ファイルのパス（EC P-256。鍵の値を環境変数に入れる口は無い）。無ければ署名しない |
| `AGENT_CARD_SIGNING_KID` | 署名の保護ヘッダの `kid`。既定は `AGENT_NAME`、それも無ければ `dak_agent` |

アルゴリズムは ES256 だけ（設定にしない）。`jku` は付けない。
`entrypoint.sh` が書いた `agent.json` に、`agent/dak_agent/server.py` が ADK の読む前（`get_fast_api_app` の前）に `agent/dak_agent/agent_card.py` の `sign_card_from_env` で署名を 1 件足す。
鍵のファイルが読めない・PEM でないときは起動を止める（設定ミスを黙って無署名にしない）。

鍵の作り方（秘密鍵と、相手に渡す公開鍵）:

```bash
openssl ecparam -name prime256v1 -genkey -noout | openssl pkcs8 -topk8 -nocrypt -out card-signing.pem
openssl ec -in card-signing.pem -pubout -out card-signing.pub.pem
```

秘密鍵はリポジトリ・設定ファイル・Issue に置かない。秘密管理（SSM など）からどう取り出して PEM ファイルにするかはデプロイ構成の仕事で、DAK はファイルのパスを受けるだけ。
鍵を替えるときは、秘密鍵のファイルと相手の設定の公開鍵を書き換えて、両方を再起動する（複数の `kid` を同時には受けない）。

**相手のカードを確かめる**: `a2a_peers` の相手ごとに、相手の公開鍵の PEM ファイルを `card_public_key_file` に書く（上の YAML）。
書いた相手だけ、任せる側が起動時に相手のカードを取って検証する（`agent/dak_agent/a2a_peer_manager.py` の `fetch_agent_card` と `check_card_signature`。`create_remote_a2a_agents` が呼ぶ）。
検証のためのカードは ADK が最初の委譲で取るものとは別に取り、`RemoteA2aAgent` には今どおりカードの URL を渡す（ADK の https・同じ origin の確かめは残る）。起動の後に取り直し・検証し直しはしない（再起動で直る）。

結果は 4 つのどれかで、同じ文を相手の sub-agent の `description` の先頭とログに出す。モデルは `transfer_to_agent` の前にこの `description` を読む:

| 文 | 意味 | ログ | エージェントから見ると |
|---|---|---|---|
| `card signature: verified (kid=<kid>)` | 設定の公開鍵で通った。`kid` は通った署名のもの | info | 設定した相手が署名したカード |
| `card signature: unsigned` | カードに署名が無い（`NoSignatureError`） | info | 相手が署名していない。成りすましかどうかは分からない |
| `card signature: invalid (<例外の名前>)` | 署名はあるが通らない。改ざん・別の鍵・壊れた署名（`InvalidSignaturesError` に限らない） | warning | 設定の鍵の持ち主のカードではない |
| `card signature: unchecked (<理由>)` | 確かめていない。理由は `no card_public_key_file`（公開鍵を書いていない）か `card not fetched: <例外の名前>`（起動時にカードを取れなかった） | info | 確かめた結果が無い |

検証の結果で接続を切らない。`invalid` と `unsigned` の相手も sub-agent に残り、任せるかはエージェントが決める（憲章の原則 1「System ENABLES, Agent DECIDES」、PBI #318 の決定）。
運用者が止めたいときは、その相手を `a2a_peers` から外す。相手の公開鍵のファイルが読めないのは設定ミスとして起動を止める。

fake-LLM の構成では、`docker-compose.test.yml` の `card-keygen` が起動のときに `tests/integration/.keys/`（gitignore）に鍵の組が無ければ作り（新しい checkout と CI ではいつも新しい組）、`agent`・`agent-peer` がその鍵で署名し、`agent-consumer` が `agent-peer` を `verified` にする。
署名なしの代表は `agent-enforcer`。`tests/integration/test_a2a.py -k signature` と `-k delegation` が確かめる。

## 8. 適合テスト（CI の a2a-tck）

`.github/workflows/ci.yml` の `integration` ジョブが、統合テストの後に、同じ fake-LLM の構成の `agent` に a2a-tck を当てる（#313）。

- 版: `A2A_TCK_REF`（ジョブの `env`）で a2a-tck のコミットを固定する。今は `263b9cfaf16a554bdfb166a7ba5b67716e946349`（§3 と同じ）
- 範囲: MUST の要件だけ（`--level must`）、JSON-RPC だけ（`--transport jsonrpc`）。下の 6 テストを外し、残りが 1 つでも落ちたら CI を失敗にする
- 結果: `reports/`（`compatibility.json`・`compatibility.html` など）を成果物 `a2a-tck-report` として残す（落ちたときも）

外すテスト（`--deselect`。DAK の中では直せない 3 要件。§3.2。2026-10-08 の利用者の判断）:

| テスト | 要件 | 外す理由 |
|---|---|---|
| `tests/compatibility/core_operations/test_artifacts.py::TestTextArtifact::test_task_has_text_artifact[jsonrpc]` | DM-ART-001 | tck のシナリオに合わせて作った SUT を前提にする（決まった中身の artifact を出させる） |
| `…::TestFileArtifact::test_task_has_file_artifact[jsonrpc]` | DM-ART-001 | 同上（ファイルの artifact） |
| `…::TestFileUrlArtifact::test_task_has_file_url_artifact[jsonrpc]` | DM-ART-001 | 同上（URL のファイルの artifact） |
| `…::TestDataArtifact::test_task_has_data_artifact[jsonrpc]` | DM-ART-001 | 同上（data の artifact） |
| `…::TestMessageResponse::test_returns_message_with_text_part[jsonrpc]` | DM-MSG-001 | Message での応答を期待する。ADK の窓口はいつも Task で返す |
| `tests/compatibility/core_operations/test_data_model.py::TestCamelCaseFieldNames::test_no_snake_case_keys` | DM-SERIAL-001 | ADK が `metadata` に入れる `adk_session_id` などの snake_case のキーを違反と数える |

手元で回すとき（fake-LLM の構成を立てた後。sb-offload でも同じ）。`--deselect` に渡す完全な ID は `ci.yml` の手順からそのまま写す（表は 4 つを `…::` で縮めている。pytest は合わない ID を黙って無視する）:

```bash
git clone https://github.com/a2aproject/a2a-tck.git && cd a2a-tck
git checkout 263b9cfaf16a554bdfb166a7ba5b67716e946349
uv sync --locked
uv run --locked ./run_tck.py --sut-host http://localhost:8000/a2a/dak_agent --transport jsonrpc --level must -- --deselect <上の 6 つ>
```

a2a-tck を上げるとき:

1. 新しいコミットで、外すテストを付けずに手元で回す（上のコマンドから `--` 以降を外す）
2. 落ちたテストを上の表と突き合わせる。表に無いテストが落ちたら、DAK のずれとして直す（外して済ませない）。表のテストの名前が変わっていたら表と `ci.yml` の `--deselect` を直す。表のテストが通るようになっていたら、外すのをやめる
3. `A2A_TCK_REF` とこの節の版を同じ PR で変える

google-adk や a2a-sdk を上げたときも、表の 3 要件が通るようになっていないかを、手元で外さずに回して確かめる。
