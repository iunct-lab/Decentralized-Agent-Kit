# 相手の見つけ方と信頼の確かめ方: 5 つのやり方の比較

PBI #318 / Task #319。DAK がほかのエージェント（A2A）と MCP サーバを見つけ、相手が本物かを確かめるやり方の候補を、
一次資料で比べる。この文書は調べた結果と試作の範囲までを扱い、コードは変えない
（試作は #320、採否は #321）。

- §1 今の DAK の見つけ方
- §2 比較表
- §3 やり方ごとの補足
- §4 試作する範囲
- §5 試作の結果
- §6 採否の案（利用者の判断待ち。PBI #318）

確認日はすべて 2026-10-08。一次資料は §2 の表の「出典」の列と §3 に書く。

## 1. 今の DAK の見つけ方

相手は設定ファイルとコンテナの環境変数に URL を手で書いて決める。相手が本物かを確かめる仕組みは無い。

| 相手 | どこに書くか | 読むところ |
|---|---|---|
| A2A の相手 | `agent/agent_config.yaml` の `a2a_peers`（`name`・`url`・`capabilities`、任意で `card_url`） | `agent/dak_agent/a2a_peer_manager.py` の `resolve_agent_card_url` がカードの URL を組み立て（`card_url`、`url` がカードの URL ならそのまま、`url` が窓口ならその下の `/.well-known/agent-card.json`、`url` にパスが無ければ DAK の既定の窓口 `/a2a/dak_agent` を補う。[a2a.md](a2a.md) §7）、`create_remote_a2a_agents` が google-adk の `RemoteA2aAgent` に渡す。`ENABLE_A2A_CONSUMER=true` のときだけ |
| MCP サーバ | `agent/agent_config.yaml` の `mcp_servers`（`name`・`url`・`type`）と、環境変数 `MCP_SERVER_URL`（`docker-compose.yml` の `agent` が `http://mcp-server:8000/mcp` を渡す） | `agent/dak_agent/config.py` の `load_agent_config`、`agent/dak_agent/adaptive_agent.py` |
| 自分のカード | `agent/entrypoint.sh` が起動時に `agent/dak_agent/agent.json` を書く。A2A 1.0 の形（`supportedInterfaces` に 1.0 と 0.3 の窓口）で、`signatures` は無い | google-adk がそれを `GET /a2a/dak_agent/.well-known/agent-card.json` で配る（[a2a.md](a2a.md) §1） |

カードの署名について、DAK が今使っている部品の事実:

- `a2a-sdk` は 1.2.2（`agent/uv.lock`）。`a2a/utils/signing.py` に `create_agent_card_signer`（JCS で正規化したカードに JWS で署名し、`signatures` に足す）と
  `create_signature_verifier`（`kid`・`jku` から鍵を引く関数を受け取り、署名が 1 つでも通れば成功）がある。PyJWT が要り（extra の `signing`）、
  DAK の venv には google-adk の依存として PyJWT 2.10.1 が入っている
- `a2a-sdk` のクライアント側は `A2ACardResolver.get_agent_card(..., signature_verifier=...)` と `ClientFactory` で検証の関数を受け取れる
- google-adk 2.11.0 の `RemoteA2aAgent`（`google/adk/a2a/agent/_remote_a2a_agent.py`）には検証の関数を渡す口が無い（signature の扱いが無い）。
  DAK の Consumer がカードの署名を確かめるには、カードを `a2a-sdk` で先に取って確かめてから ADK に渡す形になる。
  ただし ADK は、URL から取ったカードにだけ https（http はループバックだけ）とカードを取った URL と同じ origin を求め、カードのオブジェクトを直接渡されたときはこの確かめを飛ばす（`_validate_card_rpc_targets`。[a2a.md](a2a.md) §7）。
  先に取って渡す形にするなら、同じ確かめを DAK 側に残す

## 2. 比較表

「DAK のどこに入るか」は、採用した場合に触る場所。「憲章」は `docs/CHARTER.md` の設計原則
（1: System ENABLES, Agent DECIDES、2: 疎結合、4: Multi-LLM 中立・ローカル LLM でも成立）とスコープ外の「重量級の新規常時依存」に照らしたもの。

| やり方 | 標準化の段階 | 実装（言語・版・最終更新） | 依存の重さ | 手元で試せるか | 中央の登録簿 | 信頼の根拠 | DAK のどこに入るか | 憲章 | 出典（確認日 2026-10-08） |
|---|---|---|---|---|---|---|---|---|---|
| 今の設定ファイル | — （DAK の独自の設定） | DAK の `a2a_peer_manager.py` / `config.py` | 追加なし | 試せる（今の構成） | 頼らない（設定に書いた URL） | なし（HTTPS にすれば TLS の証明書だけ） | 今のまま | 違反なし。相手の正しさは運用者の手書きに頼る | [agent_config.yaml](https://github.com/iunct-lab/Decentralized-Agent-Kit/blob/57dea277c7e85b176b5a3faea562b0edea0e5051/agent/agent_config.yaml)、[a2a_peer_manager.py](https://github.com/iunct-lab/Decentralized-Agent-Kit/blob/57dea277c7e85b176b5a3faea562b0edea0e5051/agent/dak_agent/a2a_peer_manager.py)、[docker-compose.yml](https://github.com/iunct-lab/Decentralized-Agent-Kit/blob/57dea277c7e85b176b5a3faea562b0edea0e5051/docker-compose.yml)、[entrypoint.sh](https://github.com/iunct-lab/Decentralized-Agent-Kit/blob/57dea277c7e85b176b5a3faea562b0edea0e5051/agent/entrypoint.sh)（main の `57dea27`） |
| DNS-AID | IETF の個人ドラフト draft-mozleywilliams-dnsop-dnsaid-02（2026-05-27、期限 2026-11-28。作業部会に未採択、Standards Track 志望）。Linux Foundation のプロジェクト（2026-05-27 発表） | Python の `dns-aid`（dns-aid-core）0.28.1（PyPI 2026-08-09、Apache-2.0、Python 3.11 以上、draft-02 を実装。main の最終コミット 2026-09-20） | 必須の依存 7 つ（dnspython・pydantic・httpx・structlog・python-dotenv・cryptography・anyio）。DNS の各社向けは extra。`dns-aid` だけの venv で 28 MB・18 パッケージ（§5） | 試せる。dns-aid-core が BIND9 のコンテナの試験の構成（`tests/integration/bind/docker-compose.yml`、ゾーン `test.dns-aid.local`、RFC 2136 の動的更新）を持つ | 頼らない（各組織が自分のゾーンに出す。組織の一覧は `_index._agents.<domain>`） | DNSSEC（記録は SHOULD で署名、利用側は SHOULD で検証）。DANE TLSA と JWS は任意。DNSSEC が無いとリゾルバの答えを信じるだけ | `a2a_peer_manager.py` の `resolve_agent_card_url` の前に名前から窓口を引く段、`mcp_servers` の `url` の代わりに DNS 名 | 発見の結果（`dnssec_validated` など）を判断の材料として返せる。常時の依存はやや重い（Consumer だけが使う） | [datatracker](https://datatracker.ietf.org/doc/draft-mozleywilliams-dnsop-dnsaid/)、[draft-02 本文](https://www.ietf.org/archive/id/draft-mozleywilliams-dnsop-dnsaid-02.txt)、[dns-aid-core](https://github.com/dns-aid/dns-aid-core)、[PyPI dns-aid](https://pypi.org/project/dns-aid/)、[LF の発表](https://www.linuxfoundation.org/press/linux-foundation-announces-dns-aid-project-to-advance-decentralized-ai-agent-discovery) |
| A2A 1.0 の署名付き Agent Card | A2A 仕様 1.0.0 の §8.4「Agent Card Signing」（2026-03-12 公開）。署名は MAY | `a2a-sdk` 1.2.2 の `a2a/utils/signing.py`（DAK が既に使う版。§1） | 追加なし（PyJWT は DAK の venv に既にある） | 試せる（鍵を手元で作り、DAK のカードに署名して検証） | 頼らない。ただし公開鍵をどこから信じるか（`jku` の URL か、信頼する鍵の置き場）は仕様の外 | JWS（RFC 7515）+ JCS（RFC 8785）。保護ヘッダに `alg`・`typ`・`kid`、任意で `jku` | 自分のカード: `agent/entrypoint.sh` の後で署名。相手のカード: `a2a_peer_manager.py` で `a2a-sdk` の検証を通してから `RemoteA2aAgent` に渡す（ADK が URL から取るときの https と同じ origin の確かめを DAK 側に残す。§1） | 検証の結果を判断の材料として返せる。依存が増えない | [A2A specification §4.4.7・§8.4](https://a2a-protocol.org/latest/specification/)、[1.0 の告知](https://a2a-protocol.org/latest/announcing-1.0/)、`agent/.venv` の `a2a/utils/signing.py`（`a2a-sdk` 1.2.2） |
| agent:// URI | IETF の個人ドラフト draft-narvaneni-agent-uri-03（2026-04-18、期限 2026-10-20。Independent Submission、Experimental 志望、作業部会なし） | Python の `agent-uri` 0.3.0（PyPI 2025-06-25。リポジトリの最終コミット 2026-04-18、ライセンスの表示なし） | 未測（試作しない。§4） | 試せはする（`/.well-known/agents.json` を HTTP で配るだけ）が、相手が無い | 頼らない（ホスト名の `/.well-known/agents.json` か DID） | 任意: 記述子の署名（HTTP Message Signatures（RFC 9421）か JWS）、鍵は JWKS か DID。認可は OAuth | `a2a_peer_manager.py` に `agent://` の解決を足す。カードの代わりの記述子（`application/agent+json`）の読み | 違反なし。ただし A2A のカードと役割が重なる | [datatracker](https://datatracker.ietf.org/doc/draft-narvaneni-agent-uri/)、[draft-03 本文](https://datatracker.ietf.org/doc/html/draft-narvaneni-agent-uri-03)、[agent-uri](https://github.com/agent-uri/agent-uri)、[PyPI agent-uri](https://pypi.org/project/agent-uri/) |
| NANDA の Index と AgentFacts | 研究（論文）。版のついた仕様は無い。AgentFacts の JSON Schema（`projnanda/agentfacts-format`）は 2025-06-17 の 1 コミットで版の番号が無く、VC の形を含まない | `nanda-adapter` 1.0.1（PyPI 2025-07-21、リポジトリ `projnanda/adapter` の最終 push 2025-10-27）。`nanda-index-v2`（TypeScript、最終 push 2026-10-04、リリースなし） | 未測（試作しない。§4）。`nanda-index-v2` は Postgres・Fastify・Next.js の別サービス | adapter は既定でホストされた登録簿（`chat.nanda-registry.com`）に登録し、Anthropic の API キーと公開ドメインを求める。`nanda-index-v2` は手元の docker compose で動く | 頼る（論文では大学などがホストする登録簿の「quilt」。adapter は既定で中央の登録簿） | 論文では AgentFacts を W3C Verifiable Credentials 2.0 で署名、索引の記録は Ed25519 で署名。実装の schema には無い | 別サービス（索引）への問い合わせを `a2a_peer_manager.py` に足す | adapter の既定（単一ベンダの API キー、ホストされた登録簿）は原則 4 と疎結合に合わない | [arXiv 2508.03095](https://arxiv.org/abs/2508.03095)（v3 2025-10-20）、[arXiv 2507.14263](https://arxiv.org/abs/2507.14263)（2025-07-18）、[projnanda のリポジトリ](https://github.com/projnanda)、[agentfacts-format](https://github.com/projnanda/agentfacts-format)、[adapter](https://github.com/projnanda/adapter)、[nanda-index-v2](https://github.com/projnanda/nanda-index-v2)、[PyPI nanda-adapter](https://pypi.org/project/nanda-adapter/) |

## 3. やり方ごとの補足

### DNS-AID

- **owner 名は draft-02 で変わった。** PBI #318 の背景に書いた `_<名前>._<プロトコル>._agents.<ドメイン>` は draft-01 までの形。
  draft-02 の正規の記録は、エージェントの名前そのもの（例 `agent-name.example.com`）に置く SVCB の ServiceMode で、
  `_agents.<ドメイン>` の下に置くなら AliasMode で正規の名前を指す（draft-02 §3.1）。組織の一覧は `_index._agents.<ドメイン>`（§3.2）。
  `dns-aid` 0.28.1 のコードは古い形も読む。#320 は `dns-aid` が書く形をそのまま記録する
- **MCP と A2A の区別**は SVCB の `alpn`（`mcp` / `a2a`。どちらも IANA に申請中の仮の値）。
  カードの場所などは新しい SvcParam（`cap`・`cap-sha256`・`well-known` など。番号は未割り当て）で運ぶ
- **DNSSEC**: draft-02 §1.1「The records SHOULD be DNSSEC-signed」、§6.4「DNS-AID is deployable without DNSSEC, but the authenticity guarantees it offers depend on a validated path」
  「Consumers SHOULD validate DNSSEC and SHOULD refuse to act on bogus or unverifiable records」。
  `dns-aid` の `discover` は既定で DNSSEC を求めない（`require_dnssec=False`）。`True` のときは自分で検証せず、上流のリゾルバの AD ビットを見る（無ければ `DNSSECError`）。
  返り値の `DiscoveryResult` に `dnssec_validated` と `query_time_ms` がある
- **API**: `dns_aid.publish(name, domain, protocol, endpoint, port=443, ..., backend=None)` と
  `dns_aid.discover(domain, protocol=None, name=None, require_dnssec=False, ...)`。RFC 2136 の更新先は環境変数 `DDNS_SERVER`・`DDNS_PORT`・`DDNS_KEY_NAME`・`DDNS_KEY_SECRET`・`DDNS_KEY_ALGORITHM` で渡す
  （`src/dns_aid/backends/ddns.py`）。BIND9 の試験の構成はホストの 15353 番で待つ

### 署名付き Agent Card

- 仕様 1.0.0 §8.4: 「Agent Cards MAY be digitally signed using JSON Web Signature (JWS) as defined in RFC 7515」。
  正規化は JCS（RFC 8785）で、`signatures` 自体は署名の対象から外す。フィールドの残し方も決まっている: 一度も設定していない任意のフィールドは外し、
  既定値（`false` など）を明示した任意のフィールドは残し、必須のフィールドはいつも残し、それ以外の既定値のフィールドは外す（§8.4.1。例は `capabilities` の `streaming: false` を残し、空の `extensions` を外す）。
  `a2a-sdk` 1.2.2 は protobuf の `MessageToDict` の後に空の文字列・配列・オブジェクトを再帰的に外して正規化する（`_canonicalize_agent_card`）。
  この規則と細部で食い違えば、ほかの実装が付けた署名を DAK が（またはその逆が）検証できない。#320 は `a2a-sdk` どうしの往復だけを試し、ほかの実装との相互運用は試さない
  保護ヘッダは `alg`・`typ`（SHOULD で `JOSE`）・`kid` が必須で、`jku` は任意（§8.4.2）。
  クライアントは `kid` と `jku`（か信頼する鍵の置き場）で公開鍵を取って検証し、カードを信じる前に少なくとも 1 つの署名を確かめる SHOULD（§8.4.3）
- 署名はカードの中身が鍵の持ち主のものだと示すだけで、その鍵の持ち主を信じてよいかは別に決める（`jku` の URL のドメイン、DNS-AID の DNSSEC、運用者が置いた鍵）。
  DNS-AID と組み合わせると「DNSSEC で窓口とカードの場所を確かめ、カードは JWS で確かめる」形になる
- DAK は既に A2A 1.0 のカードを配り（#311）、`a2a-sdk` 1.2.2 に署名と検証の関数があるので、#320 でそのまま試せる

### agent://

- draft-03 は `agent://<authority>/<path>` と、運び方を明示する `agent+<protocol>://`（例 `agent+https`）を定める。
  authority がホスト名なら HTTPS で `/.well-known/agents.json` を取り、パスの先頭（エージェントの名前）から記述子の URL を引く。DID なら `AgentDescriptor` の service を引く
- 記述子の `skills` は A2A のカードの `skills` に合わせてあり、運び方は A2A。MCP はエージェントの中で使うものとして扱い、MCP サーバの見つけ方は定めない
- [arXiv 2508.03095](https://arxiv.org/abs/2508.03095)（#122 が参照した論文）は agent:// を定めておらず、触れてもいない（5 つの登録簿を比べる調査論文。v3 で題が「Evolution of AI Agent Registry Solutions: Centralized, Enterprise, and Distributed Approaches」に変わった）

### NANDA

- 論文（arXiv 2508.03095 v3、設計は arXiv 2507.14263）での形: 索引（Lean Index）は ID から 120 バイト以内の AgentAddr（FactsURL・AdaptiveRouterURL など。登録簿の Ed25519 署名つき）を返し、
  AgentFacts は W3C Verifiable Credentials 2.0 で署名した JSON-LD。索引は複数の登録簿をつないだ「quilt」
- 実装との差: `agentfacts-format` の JSON Schema に VC と JSON-LD の形が無い。`nanda-index-v2` の README に連携（federation）と VC の記述が無く、
  記録はほかの登録簿・Agent Card・DNS-AID の記録を指す。2507.14263 の PDF は本文を取り出せず、詳細は 2508.03095 の説明による

## 4. 試作する範囲

| やり方 | 試作 | 理由 |
|---|---|---|
| 今の設定ファイル | しない（比べる基準） | 今の構成で動いている |
| DNS-AID | **する（#320）** | 標準化の場（IETF の個人ドラフト + Linux Foundation）と、版のついた Python の実装（0.28.1）がそろい、手元の BIND9 だけで試せる。中央の登録簿に頼らない |
| 署名付き Agent Card | **する（#320）** | A2A 1.0 の仕様の一部で、DAK が使う `a2a-sdk` 1.2.2 に関数があり、依存が増えない |
| agent:// | しない | 作業部会に採択されていない Independent Submission の個人ドラフトで、-03 の期限は 2026-10-20。実装 `agent-uri` の最後のリリースは 2025-06-25 の 0.3.0。解決の中身は HTTPS で記述子を取ることで、DAK が今 `card_url` で書ける A2A のカードの場所と役割が重なる。信頼の根拠（記述子の署名）も A2A のカードの署名と同じ層 |
| NANDA の Index と AgentFacts | しない | AgentFacts に版のついた仕様が無く、公開の schema は論文の署名（VC）の形を含まない。adapter は既定でホストされた中央の登録簿と単一ベンダの API キーを求め、憲章の原則 4 と疎結合に合わない。手元で動く `nanda-index-v2` はリリースが無く、記録の先として DNS-AID の記録を指せるので、下地の DNS-AID を試せば足りる |

また見直す条件:

- **agent://**: ドラフトが IETF の作業部会に採択される、または IETF stream の文書になる。あるいは、直近 6 か月にリリースのある実装が出る。あるいは、DAK がつなぎたい相手が agent:// で窓口を出す
- **NANDA**: AgentFacts に版のついた仕様（VC の形を含む）が出る。あるいは、`nanda-index-v2` がリリースされ、中央の登録簿なしで連携できることが文書になる

## 5. 試作の結果

#320。手順とスクリプトは [`scripts/spikes/agent_discovery/`](../../scripts/spikes/agent_discovery/README.md)。
README の手順のコードブロックを、Docker の使える使い捨ての arm64 の Linux（Docker Compose v5.4.0、Python 3.12）でそのまま実行した（2026-10-08）。
DAK は fake-LLM の構成（`docker-compose.yml` + `docker-compose.test.yml` の `agent` と `mcp-server`）、DNS は dns-aid-core の v0.28.1（コミット `e3adf8b`）の
BIND9 の試験用の構成（`ubuntu/bind9:latest`、ゾーン `test.dns-aid.local`、TSIG で RFC 2136 の動的更新）。手順の全体（ビルド・起動・試作・片づけ）は 1 分 14 秒。
レビューで片づけを直した（compose のプロジェクト名を分けた）後にもう一度実行し、同じ結果だった（1 分 9 秒。`discover` は a2a 10.2 ms・mcp 2.4 ms）。

### DNS-AID の公開と発見

`publish_and_discover.py` の出力（要点）:

```
published dak-agent: success=True records=['SVCB dak-agent.test.dns-aid.local', 'TXT dak-agent.test.dns-aid.local']
published dak-mcp: success=True records=['SVCB dak-mcp.test.dns-aid.local', 'TXT dak-mcp.test.dns-aid.local']
index: success=True entries=['dak-agent/a2a', 'dak-mcp/mcp']
discover(a2a): 1 agent(s), dnssec_validated=False, query_time_ms=11.4, wall_ms=11.4
  dak-agent.test.dns-aid.local target=localhost port=8000 well_known=/a2a/dak_agent/.well-known/agent-card.json endpoint_url=https://localhost:8000
discover(mcp): 1 agent(s), dnssec_validated=False, query_time_ms=3.0, wall_ms=3.0
  dak-mcp.test.dns-aid.local target=localhost port=8001 well_known=None endpoint_url=https://localhost:8001
discover(a2a, require_dnssec=True): DNSSECError: DNSSEC validation required but the following agent FQDNs were not authenticated (AD flag not set): ['dak-agent.test.dns-aid.local']
agent card: name=dak_agent interfaces=['http://localhost:8000/a2a/dak_agent', 'http://localhost:8000/a2a/dak_agent']
mcp initialize: serverInfo={"name": "dak-agent-mcp", "version": "1.30.0"}
```

- **記録の形**: `dns-aid` 0.28.1 は draft-02 の形（エージェントの名前そのものの owner `dak-agent.test.dns-aid.local`）で SVCB を書く。
  中身は `1 localhost. mandatory=alpn,port alpn="a2a" port=8000 key65409="/a2a/dak_agent/.well-known/agent-card.json"`（`well-known` は番号が未割り当てなので私用の番号 `key65409`）と、`version=1.0.0` の TXT
- **組織の一覧が要る**: 公開しただけでは `discover(ZONE, protocol=...)` が 0 件だった。`_index._agents.<ゾーン>` の索引（`dns_aid.core.indexer.update_index`。TXT `agents=dak-agent:a2a,dak-mcp:mcp`）を書いて見つかった。
  名前を知っている相手は `discover(..., name=...)` か `discover_at_fqdn` で引ける
- **発見にかかった時間**: 1 回の `discover` が 2〜12 ms（2 回の実行）（手元の BIND9 に直接。キャッシュの効くリゾルバや本物の DNS の遅延は含まない）
- **依存の大きさ**: 新しい venv に `dns-aid==0.28.1` だけを入れて 28 MB、18 パッケージ（`dns-aid` 自身を含む）。
  DAK の agent（`agent/uv.lock`）にはこのうち httpx・pydantic・cryptography・anyio・python-dotenv が既にあり、新しく入るのは dnspython と structlog（と `dns-aid` 自身）
- **DNSSEC**: 試験用のゾーンは署名していない。既定（`require_dnssec=False`）では `dnssec_validated=False` のまま発見を返す。
  `require_dnssec=True` にすると `DNSSECError`（上流のリゾルバの AD ビットを見るだけで、`dns-aid` 自身は検証しない。§3）。署名したゾーンと検証するリゾルバでの振る舞いは試していない
- **DAK に入れるときに考え直すこと**（試作では回り道をした。README の「試作の中でしていること」）:
  - `discover` はリゾルバの向き先を引数で受け取らない（ホストの `resolv.conf` を使う）
  - `dns-aid` は窓口の URL をいつも `https://<target>:<port>` で組み立て、`well-known` からカードの URL を作るときはポートを落とす（`https://localhost/a2a/...`）。
    さらに、そのカードを取りに行くとき、ループバックの宛先を SSRF の対策で止める（出力の `Cap URI blocked by SSRF protection`）。http の手元の DAK は、記録の `target`・`port`・`well-known` から URL を自分で組み立てて取った
  - MCP の窓口のパス（DAK は `/mcp`）を載せる SvcParam が DNS-AID に無い

### 署名付き Agent Card

`sign_agent_card.py` の出力（同じ実行。DAK が配るカードに対して）:

```
served card with verifier: NoSignatureError: AgentCard has no signatures to verify.
card: name=dak_agent signatures=0
signed: signatures=1 protected=eyJhbGciOiJFUzI1NiIsImtp...
signed card: verified
signed card after JSON round trip: verified
tampered card (name=dak_agenX): InvalidSignaturesError: No valid signature found
card signed by another key with the same kid: InvalidSignaturesError: No valid signature found
```

- `a2a-sdk` 1.2.2 の `create_agent_card_signer` / `create_signature_verifier` で、DAK のカードに ES256（P-256）で署名し、検証できた。JSON にして読み直しても通る
- カードの `name` を 1 文字変えると、また同じ `kid` で別の鍵が署名すると `InvalidSignaturesError` で落ちる
- 今の DAK のカードを、検証の関数を付けた `A2ACardResolver.get_agent_card` で取ると `NoSignatureError`。Consumer で署名を求めるなら、署名の無い相手はこの例外になる（接続を切るか、判断の材料として返すかは #321）
- 鍵は実行ごとにメモリの中で作り、どこにも書いていない。鍵の置き場と配り方（`jku` の JWKS、DNS-AID の JWS など）はスコープ外（PBI #318）
- ほかの実装（a2a-sdk 以外）が付けた署名との相互運用は試していない（§3 の §8.4.1 の正規化の細部）

## 6. 採否の案

#321。**利用者の判断待ち**（PBI #318 の `## 判断待ち`）。回答を受けて、この節を「採否」に直す。

| やり方 | 案 | 根拠 | また見直す条件 |
|---|---|---|---|
| 今の設定ファイル | **続ける**（既定の見つけ方のまま） | 依存が無く、どの構成でも動く（§2）。ほかのやり方を足しても、相手を手で書く道は残す | — |
| 署名付き Agent Card | **採用**（実装の PBI を別に切る） | A2A 1.0 の仕様の一部（§2）。DAK が使う `a2a-sdk` 1.2.2 の関数で、依存を増やさずに署名と検証ができた。改ざんと別の鍵の署名を見分けた（§5） | — |
| DNS-AID | **見送り** | 作業部会に未採択の個人ドラフトで、owner 名の形が -01 と -02 で変わり、`cap`・`well-known` などの SvcParam の番号が未割り当て（§2・§3）。`dns-aid` は試作で、リゾルバの向き先を受け取らない・窓口を https に決め打ちする・MCP のパスを載せる場所が無いなど、DAK に入れるには回り道が要った（§5）。DNSSEC を求めても検証は上流のリゾルバ任せ（§5）。DAK がつなぐ相手に DNS-AID で窓口を出しているものがまだ無い | draft が dnsop の作業部会に採択される、または SvcParam の番号が IANA に割り当てられる。あるいは、DAK がつなぎたい相手が DNS-AID で窓口を出す |
| agent:// | **見送り** | §4（試作しない理由と同じ） | §4 |
| NANDA の Index と AgentFacts | **見送り** | §4（試作しない理由と同じ） | §4 |

### 署名付き Agent Card を実装する場合の入り口（案）

実装は別の PBI（PBI #318 のスコープ外）。その PBI の出発点として:

- **自分のカード**: 署名の鍵が設定されているときだけ、起動時に `agent/dak_agent/agent.json` のカードへ `create_agent_card_signer` で署名して配る。鍵が無ければ今と同じく署名しない（既定 off）。鍵の置き場と `kid`・`jku` の決め方はその PBI で決める
- **相手のカード**: `agent/dak_agent/a2a_peer_manager.py` の `create_remote_a2a_agents` で、`RemoteA2aAgent` に渡す前に `a2a-sdk` の `A2ACardResolver.get_agent_card(signature_verifier=...)` でカードを取って確かめる。
  ADK が URL から取るときにしている https と同じ origin の確かめは DAK 側に残す（§1）。信じる公開鍵は `a2a_peers` の各相手に書く（例 `card_keys`。名前はその PBI で決める）

### 検証の結果をエージェントにどう渡すか（案）

憲章の「System ENABLES, Agent DECIDES」と「Observation-Driven」に合わせ、**検証の結果で接続を自動で切らず、判断の材料として返す**:

- 相手ごとに `verified`（どの `kid` で通ったか）/ `unsigned`（`NoSignatureError`）/ `invalid`（`InvalidSignaturesError`）/ `unchecked`（信じる鍵を書いていない）を決め、
  その相手の sub-agent の説明（モデルが `transfer_to_agent` の前に読む `description`）に 1 行で書き、ログにも出す
- `invalid` の相手にも任せられる（任せるかはエージェントが決める）。運用者が止めたいときは、その相手を `a2a_peers` から外す
- DNS-AID を後で採用するなら、`dnssec_validated` も同じ形で渡す

