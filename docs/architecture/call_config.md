# 呼び出しごとの設定（`dak:` キー）

1 つの DAK を複数の用途で使い分けるために、呼び出し元は 1 回の呼び出しごとに
指示や出力の形を渡せる。何も渡さなければ、今までどおり起動時の設定で動く。
実装は `agent/dak_agent/call_config.py`（受け口と検証）と
`agent/dak_agent/adaptive_agent.py`（適用）。

## 渡し方

| 経路 | 置き場所 |
|---|---|
| ADK の `/run` / `/run_sse` | リクエストの `state_delta` |
| A2A | message の `metadata`（ADK が `RunConfig.custom_metadata["a2a_metadata"]` に入れる） |

`/run` の例:

```json
{
  "app_name": "dak_agent",
  "user_id": "u1",
  "session_id": "s1",
  "new_message": {"role": "user", "parts": [{"text": "次の問い合わせを要約して: …"}]},
  "state_delta": {
    "dak:instruction": "問い合わせを要約する。出力は JSON だけ。",
    "dak:output_schema": {
      "type": "object",
      "required": ["points"],
      "properties": {"points": {"type": "array", "items": {"type": "string"}}}
    }
  }
}
```

## キー

| キー | 値 | 指定しないとき |
|---|---|---|
| `dak:instruction` | 文字列。そのセッションのシステムプロンプトになる（既定の指示・モード指示・スキルの追記を置き換える） | `AGENT_INSTRUCTION`（とモード・スキル）のまま |
| `dak:output_schema` | JSON Schema（dict、Draft 2020-12）。LLM のリクエストに構造化出力の指定（`response_schema`、`response_mime_type=application/json`）が入り、最終応答はこのスキーマで検証される | 指定なし。今までどおりの自由形式の応答 |
| `dak:tools` | ツール名のリスト（または `{"names": [...]}`）。その呼び出しで使うツールを、組み込みのツールと既定の MCP のツールから名前で選ぶ。`[]` ならツールなし（LLM のリクエストにツールの定義が 1 つも載らない）。`{"mcp_servers": [{"url": "...", "type": "http"\|"sse"}], "names"?: [...]}` なら、呼び出し元の MCP のツールだけを使い（確認待ちなし）、既定のツールは使わない。スキル・モードによる組み立てより優先する | スキル・モードによる今までどおりの組み立て |
| `dak:inspection` | 最終応答にかける検査。`{"json_schema"?: {...}, "http"?: {"url", "timeout_seconds"?}, "mcp"?: {"url", "tool", "timeout_seconds"?}}`。落ちた応答はエラーを添えて作り直させる（下の「検査と作り直し」） | 検査なし |
| `dak:model` | LiteLLM のモデル ID（例: `bedrock/openai.gpt-5.6-luna`、`openai/gpt-5.6-luna`）。その呼び出しの LLM リクエストだけがこのモデルに向かう。運用者が `DAK_ALLOWED_MODELS` で許可したものだけ使える | `MODEL_NAME` のまま |

- `dak:instruction` の文字列はそのまま LLM に届く。ADK の `{名前}` 差し込み（セッション state の値で置き換える機能）は通さないので、`{date}` のような文字を含めてよい
- ADK は指示の後ろに、自分の識別行（`You are an agent. Your internal name is "dak_agent".`）を足す。これは既定の指示でも同じ

## モデルの許可一覧（運用者）

`dak:model` で選べるモデルは、運用者が環境変数 `DAK_ALLOWED_MODELS`（カンマ区切りのモデル ID）で決める。
呼び出し元が任意のモデルを指定して、費用の上限を破れないようにするため。

- **未設定なら、`dak:model` の指定は常に拒否する。** 許可するときだけ、運用者が明示的に設定する
- 一覧に無いモデル（または文字列でない値）を指定すると、LLM を一切呼ばずに、そのターンは次の応答で終わる

```json
{"error": "model_not_allowed", "requested_model": "openai/not-allowed", "allowed_models": ["openai/fake-alt", "openai/fake-default"]}
```

- モデルの別名（短い名前の辞書）は無い。`MODEL_NAME` と同じ形のモデル ID をそのまま書く
- 1 回のリクエストの上限（コンテキストハーネスの `request_token_budget`）は、選んだモデルのコンテキスト窓（LiteLLM のモデル表）から計算し直す。`MODEL_CONTEXT_WINDOW` は起動時の `MODEL_NAME` の窓なので、ほかのモデルには使わない。LiteLLM のモデル表に無いモデル（llama-server の別名など）は窓が分からないので、起動時のモデルの窓（`MODEL_CONTEXT_WINDOW` があればその値）を使う。窓の小さいローカルモデルを許可一覧に入れるときは、既定モデルもそれ以下の窓にしておく
- 履歴の圧縮（ADK の compaction）が始まるトークン数は、起動時の `MODEL_NAME` の窓で決まったまま変わらない（App を作るときに 1 度だけ決まるため）

## ツールの選択（`dak:tools`）

- 形が正しくない値（文字列、数値、知らないキーを持つ dict など）は、LLM を呼ばずに `{"error": "invalid_tools", ...}` で断る。制限を頼んだ呼び出しが、黙って全ツールで動くことはない
- 既定の MCP から選べるのは、その MCP に実在するツール名だけ。無い名前は無視する
- A2A の相手（`transfer_to_agent`）も、`dak:tools` の名前に含めない限り、その呼び出しでは使わない
- `dak:tools` で固定しているあいだ、`enable_skill` はツールを足さずにエラーを返す

### 呼び出し元の MCP（運用者の許可一覧）

`mcp_servers` に書ける接続先は、運用者が環境変数 `DAK_ALLOWED_MCP_URLS`（カンマ区切りの URL、完全一致）で許可したものだけ。
呼び出し元が選んだ任意の URL に agent コンテナから接続すると、運用者の内部ネットワークへの踏み台（SSRF）になるため。

- **未設定なら、`mcp_servers` の指定は常に拒否する**
- 1 つでも許可外の URL があれば、LLM を一切呼ばずに次の応答で終わる
- 許可した接続先は、完全に信頼できるものだけにする。そのツールは確認待ちなしで動く。呼び出し元の MCP への接続では HTTP のリダイレクトをたどらない（許可した接続先から内部のアドレスへ飛ばされないため）
- このスタック自身の `mcp-server`（`run_command` とファイルの読み書きを持つ）を許可一覧に入れない。既定では確認付きで使うツールを、どの呼び出し元も確認なしで使えるようになる。`docker-compose.test.yml` で入れているのは、統合テストの代役としてだけ

```json
{"error": "mcp_server_not_allowed", "requested_urls": ["http://not-allowed:9000/mcp"], "allowed_urls": ["http://mcp-server:8000/mcp"]}
```

### 呼び出し元の MCP が落ちているとき

呼び出しの始めに、各接続先のツール一覧を並行して 1 回取る（ADK の接続のタイムアウト 5 秒・再試行 1 回。最後の保険として 30 秒で打ち切り）。断る呼び出し（許可外のモデル、形の正しくない `dak:tools`）では取りに行かない。取れなかった接続先は使わずにターンを続け、次の 2 つで理由を残す。既定のツールには切り替えない（呼び出し元が MCP を明示したため）。

- セッション state の `dak:tools_error`（`/run` の応答のイベントの `stateDelta` にも出る）。接続できた呼び出しや、MCP を指定しない呼び出しで消える

```json
[{"url": "http://caller-mcp:9000/mcp", "reason": "unreachable: timed out"}]
```

- 指示の末尾の `# Unavailable tools` 節（LLM が、使えないことを踏まえて答えられるように）

`names` は、その接続先が実際に持つツール名と突き合わせる。無い名前は捨てる。

## 効く範囲と優先順位

- `state_delta` で渡した値はセッション state に書かれる。そのため、**同じセッションの以降の呼び出しにも効く**。別のセッションには効かない。解除するには、同じキーに `null` を渡す
- A2A の metadata で渡した値は、その呼び出しだけに効く（state には書かれない）
- 両方にあるときは、セッション state の値が勝つ（state > A2A metadata > `custom_metadata`）。1 つのセッションで 2 つの経路を混ぜないこと

## 検査と作り直し（`dak:output_schema` / `dak:inspection`）

最終応答（ツール呼び出しを含まないテキスト）が `dak:output_schema` か `dak:inspection` に落ちたら、DAK はこのターンの利用者の入力・落ちた応答・検査のエラーを渡して、モデルに応答を作り直させる（ツールは渡さない）。通った応答を返す。1 回目で通れば LLM の呼び出しは 1 回のまま。

- 試行は最初の応答を含めて最大 `max_llm_calls`（`dak:max_llm_calls` と運用者の `DAK_MAX_LLM_CALLS` の小さい方）、どちらも無ければ 3 回
- `dak:inspection` の検査は、応答を JSON として読んでから順にかけ、エラーをつなげる
  - `json_schema`: JSON Schema（`dak:output_schema` と同じ検証。構造化出力の指定はしない）
  - `http`: 呼び出し元のエンドポイントに応答を JSON で POST する。答えは `{"valid": true}` か `{"valid": false, "errors": [{"path": "...", "message": "..."}]}`。2xx 以外・接続できない・JSON でない答えは、検査が動かなかった扱い（下）
  - `mcp`: 呼び出し元の MCP サーバ（streamable HTTP）のツール `tool` を `{"data": <応答>}` で 1 回呼ぶ。ツールのテキストの答えは `http` と同じ形
  - `timeout_seconds` は 1 回の検査の上限（既定 10、最大 60）
- 検査そのものが動かなかったとき（エンドポイントに繋がらない・時間切れ・答えが読めない・MCP のツールのエラー）は、作り直しても直らないので作り直さず、そのエラーで失敗を返す。そのエラーには `"unavailable": true` が付く
- `http` / `mcp` の URL は、運用者が環境変数 `DAK_ALLOWED_INSPECTION_URLS`（カンマ区切りの URL、完全一致）で許可したものだけ。未設定なら検査エンドポイントは使えない（agent コンテナから呼び出し元の選んだ URL へリクエストさせないため。`DAK_ALLOWED_MCP_URLS` と同じ理由）。リダイレクトは追わない
- 許可されない URL・形の誤り・正しくない検査のスキーマは、LLM を呼ぶ前に `{"error": "inspection_url_not_allowed" | "invalid_inspection", ...}` を返して止まる

上限まで落ち続けたら、最後の応答を次の JSON に差し替えて返す。`error` は `dak:inspection` があれば `inspection_failed`、`dak:output_schema` だけなら `output_schema_validation_failed`。

```json
{
  "error": "output_schema_validation_failed",
  "attempts": 3,
  "last_response": "{\"note\": \"missing date\"}",
  "issues": [
    {"path": "date", "message": "'date' is a required property"},
    {"path": "summary/count", "message": "'three' is not of type 'integer'"}
  ]
}
```

- `path` は不正な値の位置を `/` でつないだもの。必須の項目が無いときは、その項目の名前で終わる
- JSON として読めない応答は `{"path": "", "message": "invalid JSON: ..."}`
- スキーマ自体が正しくないときは `{"path": "", "message": "invalid output_schema: ..."}`
- `$ref` はスキーマの中（`#/$defs/...`）と、JSON Schema 標準のメタスキーマだけを解決する。`http://...` などの外部の参照は取りに行かない（呼び出し元のスキーマで、agent コンテナから任意の URL へリクエストさせないため）。解決できない参照は `invalid output_schema` になる
- `{}`（何でも受け付けるスキーマ）を渡しても、応答が JSON であることは確かめる

## 検証しているテスト

- `agent/tests/test_call_scoped_instruction.py` — 指示がそのセッションのシステムプロンプトだけになる。並行する別セッションは既定のまま
- `agent/tests/test_call_scoped_output_schema.py` — リクエストに構造化出力の指定が入る。落ちた応答が作り直され、上限で構造化された失敗になる
- `agent/tests/test_call_config.py` — `dak:inspection` の形・許可一覧・JSON Schema / HTTP / MCP の検査
- `tests/integration/test_call_config_flow.py` — 実際の構成（agent コンテナ → LiteLLM → fake-LLM、呼び出し元の検査エンドポイントは `inspect-server`）で同じことを確かめる
- `tests/integration/test_minimal_overhead.py` — 指示と出力スキーマだけで `dak:tools: []` の呼び出しが、LLM を 1 回・ツール定義なし・直接呼び出しとの差 20 トークン以内で呼ぶ（`serverless.md` §2）
