# Spike: DNS-AID と署名付き Agent Card（PBI #318 / Task #320）

書き捨ての試作。本番のコンポーネントは変えない。比べた結果と判断は
[`docs/architecture/agent_discovery.md`](../../../docs/architecture/agent_discovery.md) にある。

| ファイル | やること |
|---|---|
| `publish_and_discover.py` | fake-LLM 構成の DAK の agent（A2A）と mcp-server（MCP）を、dns-aid-core の BIND9 の試験用のゾーンに DNS-AID の形（SVCB）で公開し、`dns_aid.discover` で見つけ直す。見つけた窓口から Agent Card と MCP の `initialize` の応答を取る |
| `sign_agent_card.py` | DAK が配る Agent Card に `a2a-sdk` 1.x の関数で署名し、検証する。1 文字変えたカードと、別の鍵で署名したカードが落ちることも見る |

## 要るもの

Docker（compose）、[uv](https://docs.astral.sh/uv/)、git。ポートは 8000・8001（DAK）と 15353（BIND9）を使う。

## 手順

リポジトリの直下で実行する。

```bash
# 1. dns-aid-core を一時ディレクトリに clone し、同梱の BIND9 の試験用の構成を立てる
W=$(mktemp -d)
git clone -q --depth 1 --branch v0.28.1 https://github.com/dns-aid/dns-aid-core "$W/dns-aid-core"
BIND="$W/dns-aid-core/tests/integration/bind"
chmod -R a+rwX "$BIND/zones"            # BIND が動的更新のジャーナルを書く
docker compose -f "$BIND/docker-compose.yml" up -d    # --wait は使わない（同梱の healthcheck の dig がイメージに無く、unhealthy になる）
until docker logs dns-aid-bind9 2>&1 | grep -q " running$"; do sleep 1; done

# 2. DAK を fake-LLM の構成で立てる（agent は 8000、mcp-server は 8001）
touch .env
docker compose -f docker-compose.yml -f docker-compose.test.yml up -d --build --wait agent mcp-server

# 3. dns-aid だけを入れた venv を作り、依存の大きさを測る
uv venv -q -p 3.12 "$W/venv"
VIRTUAL_ENV="$W/venv" uv pip install -q dns-aid==0.28.1
du -sh "$W/venv"; VIRTUAL_ENV="$W/venv" uv pip list | tail -n +3 | wc -l

# 4. 公開と発見。TSIG の鍵は dns-aid-core の試験用の値を、ファイルに写さず環境変数で渡す
export DDNS_KEY_NAME=dns-aid-key
export DDNS_KEY_SECRET="$(sed -n 's/.*secret "\(.*\)".*/\1/p' "$BIND/named.conf")"
"$W/venv/bin/python" scripts/spikes/agent_discovery/publish_and_discover.py

# 5. カードの署名と検証（agent の環境。a2a-sdk 1.x と PyJWT が入っている）
(cd agent && uv sync -q && uv run python ../scripts/spikes/agent_discovery/sign_agent_card.py)

# 6. 片づけ
docker compose -f docker-compose.yml -f docker-compose.test.yml down -v
docker compose -f "$BIND/docker-compose.yml" down -v
rm -rf "$W"
```

`publish_and_discover.py` の環境変数（既定値）: `DDNS_SERVER`（`127.0.0.1`）、`DDNS_PORT`（`15353`）、`DNSAID_ZONE`（`test.dns-aid.local`）、
`DAK_HOST`（`localhost`）、`DAK_AGENT_PORT`（`8000`）、`DAK_MCP_PORT`（`8001`）。
`sign_agent_card.py` は `--base-url`（既定 `http://localhost:8000/a2a/dak_agent`）か、`--card-file <JSON>` でカードを読む。

## 試作の中でしていること（本番に持ち込むなら考え直すもの）

- `dns_aid.discover` はリゾルバの向き先を引数で受け取らず、ホストの `resolv.conf` から作る。試作は dnspython の `Resolver` を差し替えて、すべての問い合わせを試験用の BIND9（ポート 15353）に向ける
- `dns-aid` は窓口の URL をいつも `https://<target>:<port>` で組み立てる。手元の DAK は http なので、試作は記録の `target`・`port`・`well-known` から URL を組み立てる
- MCP の窓口のパス（DAK は `/mcp`）を載せる SvcParam は DNS-AID に無い。試作は DAK の既定の `/mcp` を足す
- 署名の鍵は実行ごとにメモリの中で作り、どこにも書かない
