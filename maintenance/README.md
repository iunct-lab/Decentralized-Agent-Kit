# dak-maintenance

DAK が自分自身を保守するためのツールキット（要件2/3のドッグフード実装）。

- `semver` — Dependabot PR の from/to バージョンから bump レベルを判定（Tier0, LLM不要）
- `changelog` — GitHub Releases / PyPI から changelog を取得
- `risk` — changelog のリスク評価。ヒューリスティック（LLM不要）と LLM の両対応
- `decide` — bump × CI結果 × リスク → `auto-merge` か `needs-human-review` を理由付きで返す
- `llm_client` — **provider 中立**な `complete()`（Gemini/Ollama/OpenAI/… を `MAINT_LLM_*` で実行時選択）
- `search` — Web 検索（**Tavily API**。LLM とは分離。規約遵守のためスクレイピングはしない）
- `watch` / `feature` / `charter` — tech-watch / feature-sync / charter-review の提案パイプライン
- `model_compare` — 固定の入力（`tests/fixtures/model_compare/`）で複数のモデルに保守のプロンプトを投げて比べる
- `cli` — `dak-maint {triage,watch,feature-sync,collect-deps,charter-review,compare-models}`（ワークフローから呼ぶ。`collect-deps` は `gh pr list --json body` の出力を標準入力で受け、feature-sync に渡す依存の一覧を出す）

同じロジックは `agent/skills/dependency-maintenance/` の DAK スキルからも利用でき、
DAK 自エージェントが対話的にトリアージを実行できる。

## 使い方

```bash
cd maintenance && uv sync
uv run pytest -q

# 単発トリアージ（LLMなし・ヒューリスティックのみ）
uv run dak-maint triage --package litellm --from 1.51.0 --to 1.52.3 --ci-passed true

# provider 中立の LLM（例: ローカル Ollama。Gemini/OpenAI も同様に BASE_URL/MODEL を変えるだけ）
export MAINT_LLM_BASE_URL="http://localhost:11434/v1"
export MAINT_LLM_MODEL="llama3.1:8b"
export MAINT_LLM_API_KEY="ollama"
uv run dak-maint watch --charter ../docs/CHARTER.md --max-items 2   # 新技術提案 JSON
gh pr list --state merged --search 'label:deps' --limit 30 --json body | uv run dak-maint collect-deps   # 更新された依存の一覧
```

### モデルを比べる（compare-models）

保守に使うモデルを選ぶとき、同じ固定の入力（憲章の写し・検索結果の写し・依存の changelog の抜粋。`tests/fixtures/model_compare/`）で
watch / feature-sync / charter-review / triage のプロンプトを各モデルに投げ、JSON として読めたか・件数・秒数・トークン数・概算費用を Markdown の表にする。
Web 検索（Tavily）と changelog の取得はしない。モデルを呼ぶので、有料の API なら費用がかかる（候補と価格は `docs/maintenance/model-choice.md`）。

```bash
cat > models.json <<'JSON'
[{"name": "luna", "base_url": "https://api.openai.com/v1", "model": "gpt-6-luna", "api_key_env": "OPENAI_API_KEY"},
 {"name": "ollama", "base_url": "http://localhost:11434/v1", "model": "llama3.1:8b", "api_key_env": ""}]
JSON
echo '{"luna": {"input": 0.10, "output": 0.50}}' > prices.json   # 1M トークンあたりの USD。無いモデルは「価格未指定」
uv run dak-maint compare-models --models models.json --prices prices.json --out compare.md   # --cases watch,triage で絞れる
```

鍵はファイルに書かず、`api_key_env` に名前を書いた環境変数から読む。呼び出しが失敗したケース（`temperature` を受け付けない 400 など）は表の `error` に残して次へ進む。
トークン数は応答の `usage`（Bedrock は Converse の `usage`）から取り、返さないモデルは「—」。
`JSON` は各段の応答が JSON として読めたかだけを見る。形の違う JSON（watch のクエリ生成に配列でなくオブジェクトを返すなど）は処理が捨てるので、`yes` で件数 0 になる。件数 0 の行は、表の下の出力とトークン数（後の段が呼ばれたか）も合わせて読む。`--prices` の形が違えば、モデルを呼ぶ前に exit 2 で止まる。triage は既存の評価器がオブジェクトの答えだけを読むので、配列で答えると `no` になる（error に `'list' object has no attribute 'get'`）。watch と feature-sync は、定期実行（2 件まで）と違い 50 件まで数えて並べる（charter-review は処理そのものが 1 件にまとめる）。`--prices` / `--out` が読めない・書けないときも、モデルを呼ぶ前に exit 2。固定入力は `tests/fixtures/` から読むので、ソースのまま `uv run` で動かす（wheel には入らない）。

判定は「Tier0(semver+CI) で大半を決め、曖昧な時だけ LLM に委ねる」設計。
`--assessor llm` で triage のリスク評価も LLM 化。reasoning 系（watch/feature-sync/charter-review）は
`MAINT_LLM_*` 未設定なら提案 0 件を返す（失敗しない）。Web 検索は Tavily（`TAVILY_API_KEY`）。
`watch` を実際に候補生成させるには Tavily キーが必要（未設定なら検索 0 件＝提案 0 件）。
