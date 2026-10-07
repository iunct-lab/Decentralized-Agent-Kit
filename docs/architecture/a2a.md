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

調べたもの（2026-10-07）:

- 仕様: [A2A specification](https://a2a-protocol.org/latest/specification/)（v1.0、2026-03-12 公開。[告知](https://a2a-protocol.org/latest/announcing-1.0/)）。
  Agent Card のフィールドは a2a-tck が同梱する `specification/a2a.proto` の `AgentCard` / `AgentInterface` / `AgentCapabilities` と照合した
- 公式 Python SDK `a2a-sdk`: サーバ側は今の main の 0.3.26 と、書き捨ての枝で上げた 1.2.2（`>=1.1,<2` で解決した版）。
  クライアントは 1.2.2（1.0 の JSON-RPC）と 0.3.26（古い DAK が使う版）
- 適合テスト: [a2a-tck](https://github.com/a2aproject/a2a-tck) のコミット `263b9cfaf16a554bdfb166a7ba5b67716e946349`
- DAK 側: google-adk 2.11.0 の `google/adk/cli/fast_api.py`（`a2a=True` の組み立て）と `google/adk/a2a/_compat.py`、
  `agent/entrypoint.sh`、`agent/dak_agent/a2a_peer_manager.py`
- 構成: fake-LLM の構成（`docker-compose.yml` + `docker-compose.test.yml` の `agent` と `fake-llm`）。窓口は `http://localhost:8000/a2a/dak_agent`

## 1. 窓口の場所と今の版

| 項目 | 場所 |
|---|---|
| Agent Card | `GET /a2a/<AGENT_NAME>/.well-known/agent-card.json`（`agent/entrypoint.sh` が起動時に `agent/dak_agent/agent.json` を書き、ADK がそれを読んで配る） |
| JSON-RPC | `POST /a2a/<AGENT_NAME>` |
| 組み立て | `agent/dak_agent/server.py` の `get_fast_api_app(..., a2a=True)`。ADK が `agents_dir` の下で `agent.json` のあるディレクトリごとに窓口を足す（`_compat.attach_a2a_routes_to_app`、`prefix=/a2a/<ディレクトリ名>`） |

今の版: `a2a-sdk` 0.3.26（`agent/pyproject.toml` は `a2a-sdk>=0.2.0,<1`、`.github/dependabot.yml` は major を止めている）。
カードは 0.3 の形で、`"protocolVersion": "0.2.6"` を宣言する。

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
| Consumer のカードの場所 | `a2a_peer_manager.py` の `create_remote_a2a_agents` が `peer.url + "/a2a/dak_agent/.well-known/agent-card.json"` と決め打ち。相手の `AGENT_NAME` が `dak_agent` でないとつながらない | カードの URL は相手が決める（既定は窓口の下の `/.well-known/agent-card.json`） | #312 |
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

### 3.1 今の main（`a2a-sdk` 0.3.26、0.3 の形のカード）

```
E   Failed: Agent card declares no supportedInterfaces — cannot determine which transports to test
3 failed, 7 passed, 30 deselected, 225 errors in 1.84s
"must_compatibility": "5.3%"
```

| 要件 | 結果 | 理由 |
|---|---|---|
| CARD-DISC-001 | PASS | カードは取れる |
| CARD-STRUCT-001 | FAIL | `Missing required fields: {'supportedInterfaces'}` |
| CARD-PROTO-001 | FAIL | `supportedInterfaces must be a list` |
| BIND-FIELD-001 | FAIL | `At least one protocol binding must be declared` |
| それ以外（CORE-*、DM-*、STREAM-*、JSONRPC-* など） | NOT TESTED | カードに窓口が無く、クライアントを作れない（225 件の error） |

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

MUST の要件 113 件のうち、PASS 55 件・FAIL 3 件・SKIPPED 33 件・NOT TESTED 22 件（SKIPPED と NOT TESTED は、gRPC・HTTP+JSON の窓口、push 通知、拡張カードなど、カードが宣言していない機能）。FAIL の 3 件:

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
- 0.3 の相手を切り捨てる理由が無い。混在している間に DAK 同士の委譲が切れるのを避けられる

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
