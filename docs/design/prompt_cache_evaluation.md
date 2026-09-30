# プロンプトキャッシュ（ContextCacheConfig）の採否

PBI #94 / Task #226。ADK の `ContextCacheConfig` を DAK で既定で有効にするかを、プロバイダごとに決める。
根拠は #224 のテスト（`agent/tests/test_context_cache_interaction.py`）と、ADK / LiteLLM のソースを読んだ結果、偽の HTTP サーバに向けて実際に送った本文。
実プロバイダでのトークン数と遅延の実測（#225）は課金の承認待ちで、まだ無い（「未検証事項」）。

調べた版: google-adk 2.8.0、litellm 1.102.0。パスは `agent/.venv/lib/python3.12/site-packages/` からの相対（`<adk>` = `google/adk`、`<litellm>` = `litellm`）。

## 1. 背景: 設定がリクエストに届くまで

- `ContextCacheConfig` は App 全体の設定（`<adk>/agents/context_cache_config.py`、実験的機能）。今の `agent/dak_agent/agent.py` の `App(...)` は渡していないので、DAK ではキャッシュの印は一度も付いていない
- `ContextCacheRequestProcessor`（`<adk>/flows/llm_flows/context_cache_processor.py`）は、invocation の `context_cache_config` を毎回 `llm_request.cache_config` に写す。あわせてセッションのイベントから、同じエージェントの直前の応答のプロンプトのトークン数（`cacheable_contents_token_count`。次の `min_tokens` の判定の入力）と `cache_metadata` を拾う。モデルの種類も圧縮の有無も見ない
- DAK のモデルはどのプロバイダでも ADK の `LiteLlm`（`agent.py` の `model = LiteLlm(...)`）。`LiteLlm` は `cache_config` が解決されれば（`<adk>/models/_prompt_cache.py` の `resolve_cache_config`。前回のプロンプトが `min_tokens` 未満なら付けない）、プロバイダを問わず `cache_control_injection_points` を 2 点積む（`<adk>/models/lite_llm.py:1081-1110, 3120-3129`）: system 指示と、最後のメッセージ
- ADK には Gemini 専用の `GeminiContextCacheManager` もあるが、使うのは ADK の Gemini モデルクラス（`<adk>/models/google_llm.py`）だけで、`LiteLlm` 経由の DAK では使われない
- 実際に効くかは LiteLLM のプロバイダごとの変換で決まる。次の表はその結果

## 2. プロバイダ別比較

「送信本文」は、`LiteLlm` に `cache_config` を載せたリクエストを偽の HTTP サーバへ送って見た事実（ローカル LLM の 2 経路は #224 のテストで固定。クラウドの経路は同じ方法で手元で確かめた）。

| 経路（`MODEL_NAME` の例） | 送信本文 | 仕組み（ソース） | トークン・遅延の実測 |
|---|---|---|---|
| Anthropic 直（`anthropic/claude-…`） | system と最後のメッセージに `cache_control: {"type": "ephemeral"}` が付く | Anthropic のプレフィックスキャッシュ（印を付けた所までを再利用） | 未検証（#225） |
| Gemini 直（`gemini/gemini-…`） | 印の付いた範囲が 1024 トークン以上なら、先に `cachedContents` を GET（同じ内容の資源を探す）/ POST（作成）し、その資源を指して生成する。短ければ印は消えて通常の生成 | `<litellm>/llms/vertex_ai/context_caching/vertex_ai_context_caching.py` の `check_and_create_cache` と `transformation.py` の `separate_cached_messages`: 印の付いたメッセージのうち**先頭から連続したもの**だけを資源にする。system と最後のメッセージが隣り合う最初のリクエスト（`[system, user]`）だけは両方が資源に入るが、2 回目以降は離れているので、資源は **system 指示とツール定義だけ**になり、会話は資源を指した通常の contents で送られる（`[system, user, model, user]` で確かめた）。資源の名前は中身のハッシュなので、system とツールが変わらない限り同じ資源に当たる | 未検証（#225） |
| Bedrock の Claude（`bedrock/us.anthropic.claude-sonnet-4-5-…`、Converse） | system と最後のメッセージに `cachePoint` ブロックが付く | `<litellm>/llms/bedrock/chat/converse_transformation.py`（`cache_control` → `cachePoint`） | 未検証（#225） |
| Bedrock の GPT-5.6 Luna（`bedrock/us.openai.gpt-5.6-luna`、Converse。`docs/getting-started/bedrock.md` の書き方） | `cachePoint` は付かない（印は消える） | `<litellm>/llms/bedrock/common_utils.py` の `bedrock_model_accepts_cache_points`: LiteLLM のモデル表でこのモデルの Converse の項目は `supports_prompt_caching` が無いので送らない。表でキャッシュ対応なのは `bedrock_mantle/openai.gpt-5.6-luna`（Responses API）の項目だけ。これは litellm 1.102.0 に同梱の表での結果で、LiteLLM は既定では起動時にリモートの表を取りに行く（`LITELLM_LOCAL_MODEL_COST_MAP` が未設定のとき。`<litellm>/litellm_core_utils/get_model_cost_map.py`）ので、表が更新されれば版を変えなくても `cachePoint` が送られ始めうる。2026-09-21 の PBI #94 の決定ログ（モデルカード: Luna のキャッシュは Responses API だけで効き、Converse では効かない）と一致する | 未検証。Responses API 経路への切り替えは別に要る（「未検証事項」） |
| OpenAI 直（`openai/gpt-…`、api.openai.com） | 印は消える | `<litellm>/llms/openai/chat/gpt_transformation.py:422-435` の `_should_preserve_cache_control_for_endpoint`: openai.com のホストでは除く | 未検証。OpenAI は印に依らず自動でキャッシュするので、`ContextCacheConfig` の有無は関係しない見込み |
| ローカル: Ollama（`ollama_chat/llama3.1:8b`、`docker-compose.local-llm.yml`） | 印は消える（テスト `test_local_llm_routes[ollama_chat…]`） | `<litellm>/llms/ollama/chat/transformation.py` の `OllamaChatConfig.transform_request` が role と content からメッセージを作り直す（`gpt_transformation.py` は通らない。Task #224 本文の推定とは理由が違い、結論は同じ） | 対象外（課金も印も無い） |
| ローカル: llama.cpp（`openai/llamacpp` + 独自の `OPENAI_API_BASE`、`docker-compose.llamacpp.yml`） | 印が残り、`cache_control` がそのまま llama-server に届く（テスト `test_local_llm_routes[openai/llamacpp…]`） | 同じ `_should_preserve_cache_control_for_endpoint` が、openai.com 以外のホストでは残す | 未検証: llama-server がこの未知のフィールドを無視するか拒むか。llama-server はもともと前回と同じ先頭部分の KV を再利用するので、印で得るものは無い |

## 3. 圧縮との相互作用

- **配線は壊れない**: 圧縮（`EventsCompactionConfig` + `BudgetedEventSummarizer`）が 3 ターンで 2 回以上走っても、要約の前後を問わずエージェントの全リクエストに同じ `cache_config` が載る（テスト `test_cache_config_survives_compaction`）。DAK の要約器が自分で作るリクエストには載らない（`cache_config` は `None`）ので、要約の呼び出しがキャッシュを書くことは無い
- **ヒットは別問題**: プレフィックスキャッシュは先頭から一致する所までしか効かない。DAK には毎ターン先頭付近を書き換える仕組みが 3 つある。どれもこの PBI では変えない（プレフィックスの規律は #105）
  1. 圧縮: 古いイベントを要約に置き換えるので、要約の直後のターンは system 指示より後が全部ミスになる
  2. 古いツール結果の刈り込み（`harness.py` の `prune_old_tool_results`）: 直近の数ターン（`prune_protect_user_turns`、既定 2）より前で、保護するトークン数（`HarnessSettings.prune_protect_token_budget`。既定は窓の 2 割、`DAK_PRUNE_PROTECT_TOKENS` で固定できる）を超えた分の結果をポインタに置き換える。刈り込む境界がターンごとに進むので、そこから後ろがミスになる
  3. system 指示の末尾の計画と最初の依頼（`adaptive_agent.py` の `_verbatim_sections`）: `write_todos` で計画が変わると system 指示そのものが変わり、system の印もミスになる
- Anthropic / Bedrock の Claude では、ミスしたターンは書き込み（Anthropic の料金表では通常の入力より高い）だけが増える。圧縮の頻度が高い小さい窓ほど損になりうる
- Gemini では、資源は（最初のリクエストを除き）system 指示とツール定義だけなので、圧縮や刈り込みでは作り直さない。作り直すのは 3 の計画の書き換えなどで system 指示が変わったとき、と最初のリクエストの後の 1 回（資源の中身が `[system, user]` から system とツールだけに変わる）。そのたびに作成の往復と、資源の保存の料金がかかる

## 4. 既定有効化の採否

**決定: 既定では有効にしない（`agent.py` の `App(...)` に `context_cache_config` を渡さない今のまま）。** プロバイダごとの理由:

| 経路 | 採否 | 理由 |
|---|---|---|
| ローカル LLM（Ollama / llama.cpp） | 無効 | Ollama では印が消えて何も起きない。llama.cpp では未知のフィールドを送るだけで、llama-server は印なしで先頭の KV を再利用している。得るものが無く、受理されるかも未検証 |
| Bedrock の GPT-5.6 Luna（Converse） | 無効 | LiteLLM が印を送らない。有効にしても `min_tokens` の判定と印の付与だけが増える。効かせるには Responses API 経路への切り替えが要り、それは別の判断 |
| OpenAI 直 | 無効 | 印は消える。自動のキャッシュは設定に依らない |
| Anthropic 直・Bedrock の Claude | 暫定で無効 | 印は届くが、効果（読み込みトークンの割合、遅延）の実測が無い。ミスしたターンは書き込みの割増しがかかり、DAK には 3 章の書き換えがあるので、実測なしに得と言えない |
| Gemini 直 | 暫定で無効 | 資源は system 指示とツール定義だけで、会話の部分は割り引かれない。system が 1024 トークンに満たなければ何も起きず、計画が変わるたびに資源を作り直す。作成の往復と保存の料金が読み込みの割引を上回らないかは実測が要る |

有効にすると決めたときの配線（この PBI では実装しない）:

- `agent.py` で、経路が印を活かす（上の表で「印が届く」）ときだけ `App(..., context_cache_config=ContextCacheConfig(...))` を渡す。判定は `HarnessSettings.from_env(formatted_model_name)` と同じく `formatted_model_name` から行い、利用者が環境変数で切れるようにする（既定は off のまま、実測で得と分かった経路だけ on にする）
- `min_tokens` は、Anthropic / Bedrock の最小キャッシュ長と Gemini の 1024（LiteLLM の `is_prompt_caching_valid_prompt`）を下回るリクエストで印を付けない値にする
- Gemini は、2 回目以降は ADK の既定のままで system とツール定義だけが資源になる。印を system だけにして最初のリクエストの資源（`[system, user]`）を作らない案も比べる。ADK の `LiteLlm` は `cache_control_injection_points` を構築時に渡せばそちらを優先する（`lite_llm.py:3120-3126`）。計画を system 指示の外に出すかは #105

## 5. 未検証事項

- **実プロバイダでのトークン数と遅延（#225）**: Anthropic・Gemini（可能なら Bedrock）で、キャッシュの有無ごとに 2 ターン以上を送って `usage_metadata`（キャッシュの読み込み・書き込みトークン）と壁時計の時間を比べる。課金が発生するので利用者の承認待ち。承認が無い間、上の比較表の実測欄は空のまま
- **Bedrock の GPT-5.6 Luna の Responses API 経路**: LiteLLM では `bedrock_mantle/openai.gpt-5.6-luna` の経路になる。DAK がこの経路で動くか（ツール呼び出し、ストリーミング）とキャッシュの効果の実測は、着手前に利用者の承認を得てから行う別の Task とする
- **llama-server が `cache_control` を受け付けるか**: 今の送信本文では届く。無視されるのか、エラーになるのかは実機で確かめていない
- **圧縮・刈り込み・計画の書き換えで実際にどれだけミスするか**: 3 章の見立ては仕組みからの推定。ヒット率は #225 の実測か、#105 のプレフィックスの規律と合わせて測る
- Gemini の暗黙のキャッシュ（印が無くても効くもの）と OpenAI の自動キャッシュが DAK の会話でどれだけ効くかは、この PBI の比較の外（`ContextCacheConfig` の有無で変わらない）
