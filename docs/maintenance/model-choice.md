# 保守の定期実行に使うモデルの選び方

保守の定期実行（`tech-watch` / `feature-sync` / `charter-review` / `dependency-triage` の LLM 評価）は、
リポジトリの変数 `MAINT_LLM_BASE_URL` / `MAINT_LLM_MODEL` と secret `MAINT_LLM_API_KEY` でモデルを選ぶ
（`maintenance/src/dak_maintenance/llm_client.py` の `make_complete`）。この文書は候補の価格・無料枠・月額の見積もりを比べ、
切り替えと元に戻す手順を書く。どのモデルに決めたかは「結果」の節。

## 候補（価格は 1M トークンあたりの USD、入力 / 出力、標準、短いコンテキスト）

| モデル | 入力 | 出力 | 無料枠 | 無料枠のデータの扱い | 無料枠の回数制限 | 出典・確認日 |
|---|---|---|---|---|---|---|
| `gemini-3.5-flash` | $1.50 | $9.00 | あり | Google の製品改善に使われうる（有料枠は使われない） | 未確認（AI Studio の画面でだけ見られる） | [Gemini API pricing](https://ai.google.dev/gemini-api/docs/pricing)、2026-10-01 確認 |
| `gemini-3.5-flash-lite` | $0.30 | $2.50 | あり | 同上 | 未確認（同上） | 同上 |
| `gemini-3.8-flash` | $0.75（2026-12-31 まで）、$1.50（2027-01-01 から） | $3.75（2026-12-31 まで）、$7.50（2027-01-01 から） | あり（期限つき。2026-09-24 の確認では 2026-12-31 まで） | 同上 | 未確認（同上） | 同上 |
| `gpt-6-luna` | $0.10 | $0.50 | なし | API の入出力は学習に使われない（明示して共有しない限り）。不正利用の監視ログは最長 30 日 | — | [OpenAI API pricing](https://developers.openai.com/api/docs/pricing)、[Your data](https://developers.openai.com/api/docs/guides/your-data)、2026-10-01 確認 |
| `gpt-5.6-luna` | $0.20 | $1.20 | なし | 同上 | — | 同上 |
| `gpt-5-nano` | $0.05 | $0.40 | なし | 同上 | — | 同上 |
| Ollama `llama3.1:8b` | $0 | $0 | —（自前のホスト） | 外に出ない | ホストの性能しだい | 自前で動かすので価格表なし。Actions から届くホストが要る |

- 回数制限の一次資料は「Rate limits depend on a variety of factors (such as your usage tier) and can be viewed in Google AI Studio.」（[Rate limits](https://ai.google.dev/gemini-api/docs/rate-limits)、2026-09-02 更新）で、数値は載っていない。
- 推論するモデルは、見えない推論のトークンも出力として課金されうる。下の見積もりは本文の出力だけで数えているので、推論の多いモデルほど実際は高い。
- `make_complete` は常に `temperature: 0` を送る。受け付けるかはモデルごとに違い未確認で、切り替える前に 1 回呼んで確かめる（「切り替える」の 2）。Bedrock の経路は `temperature` を送らない。
- Amazon Bedrock（IAM、API キー不要。`MAINT_LLM_MODEL=bedrock/<id>`）の道もある（`docs/maintenance/README.md` の「LLM プロバイダ設定」）。Bedrock 上の価格は未確認で、この表に入れていない。

## 月額の見積もり

回数（2026-09-01〜2026-10-01 の実績から）:

| ワークフロー | 回数 / 月 | 1 回あたりの LLM 呼び出し | 呼び出し / 月 | 1 呼び出しの入力の目安 | 入力 / 月 |
|---|---|---|---|---|---|
| `tech-watch`（毎月 1 日・15 日） | 2 | 2（クエリ生成 1 + 評価 1、`watch.py`） | 4 | 生成 2.5k、評価 18k（憲章 + 検索 25 件 × 約 600 字） | 41k |
| `feature-sync`（毎週月曜） | 4.3 | 依存ごとに 1（`feature.py`。`collect-deps --max-items 10` で週 10 件まで。月 47 件あったので上限に当たる） | 43 | 10.8k（憲章 + changelog 8,000 字まで） | 464k |
| `charter-review`（四半期） | 1/3 | 1（`charter.py`。検索 5 クエリ × 3 件） | 0.33 | 11.8k | 4k |
| `dependency-triage`（Dependabot の PR ごと） | 18 回の実行（PR 14 件・47 パッケージ。PR の更新でも走る） | パッケージごとに 1（`risk.py`） | 60（47 × 18 / 14） | 8.5k（changelog 8,000 字まで） | 512k |

- トークン数は 1 字 ≈ 1 トークンで数えた（日本語の多い憲章に合わせた多めの見積もり）。出力は 1 呼び出し 0.3k〜1.5k。
- 合計: triage を除くと入力 約 0.51M（41k + 464k + 4k）/ 出力 約 0.03M、triage を含むと入力 約 1.02M / 出力 約 0.05M（feature-sync と triage が大半）。

| モデル | triage を除く | triage を含む |
|---|---|---|
| `gemini-3.5-flash` | $1.03 | $2.02 |
| `gemini-3.5-flash-lite` | $0.23 | $0.44 |
| `gemini-3.8-flash`（2026-12-31 まで / 2027-01-01 から） | $0.49 / $0.99 | $0.97 / $1.94 |
| `gpt-6-luna` | $0.07 | $0.13 |
| `gpt-5.6-luna` | $0.14 | $0.27 |
| `gpt-5-nano` | $0.04 | $0.07 |
| Ollama `llama3.1:8b` | $0（ホストの費用は別） | $0 |

Gemini の無料枠に収まれば $0 だが、回数制限が未確認なので収まるかは分からない（feature-sync と triage は 1 回の実行で最大 10 回ほど続けて呼ぶ）。

## 切り替える

鍵の値はファイルにもコマンドの履歴にも残さない（`gh secret set` は値を標準入力から読む）。手元のシェルの環境変数を使うので、1〜4 は同じシェルで続けて行う。

1. 今の値を控える: `gh variable get MAINT_LLM_BASE_URL`、`gh variable get MAINT_LLM_MODEL`（2026-10-01 時点は Gemini の OpenAI 互換の URL と `gemini-3.5-flash`）、`gh variable get MAINT_ASSESSOR`（無ければ「無し」と控える。2026-10-01 時点は無し）。secret は読み出せないので、元の鍵がどこにあるかを控える。
2. 変える前に、手元で同じ URL・モデル・鍵で 1 回だけ呼んで確かめる（数トークン分の費用がかかる。Issue は作らない）。鍵は画面に出さずに読み、ファイルに書かない:
   ```bash
   export MAINT_LLM_BASE_URL="https://api.openai.com/v1"   # Gemini なら https://generativelanguage.googleapis.com/v1beta/openai
   export MAINT_LLM_MODEL="<model id>"
   read -rs MAINT_LLM_API_KEY && export MAINT_LLM_API_KEY  # 値を貼って Enter（表示されない）
   curl -sS "$MAINT_LLM_BASE_URL/chat/completions" -H "Authorization: Bearer $MAINT_LLM_API_KEY" -H "Content-Type: application/json" \
     -d "{\"model\": \"$MAINT_LLM_MODEL\", \"temperature\": 0, \"messages\": [{\"role\": \"user\", \"content\": \"Reply with []\"}]}"
   ```
   `choices` が返ること。`400`（`temperature` を受け付けないなど）や `401` なら切り替えない。定期実行のワークフローを手で回すと、LLM を何度も呼び、提案があれば Issue を起票するので、確かめには使わない。
3. 同じ値で Actions の変数と secret を変える（同じシェルで続ける）:
   ```bash
   gh variable set MAINT_LLM_BASE_URL --body "$MAINT_LLM_BASE_URL"
   gh variable set MAINT_LLM_MODEL --body "$MAINT_LLM_MODEL"
   printf '%s' "$MAINT_LLM_API_KEY" | gh secret set MAINT_LLM_API_KEY
   ```
4. `dependency-triage` でも LLM に判定させるなら、Dependabot 側の secret にも同じ鍵を入れる: `printf '%s' "$MAINT_LLM_API_KEY" | gh secret set MAINT_LLM_API_KEY --app dependabot`。
   - Dependabot の PR で動く `dependency-triage` には、Actions の secret は渡らず、Dependabot の secret が渡る。リポジトリの変数（`vars.*`）は渡る（2026-09-28 の Dependabot の実行のログで `MAINT_LLM_BASE_URL` / `MAINT_LLM_MODEL` に値があった）。変数は Actions と共通なので、Dependabot 側に別に置くものは無い。
   - `dependency-triage.yml` は `vars.MAINT_ASSESSOR` が無ければ、`MAINT_LLM_BASE_URL` と `MAINT_LLM_MODEL` があるだけで LLM で判定する。Dependabot の secret が無いと鍵なしで呼び、失敗してパッケージごとにヒューリスティックに落ちる（同じ実行で 503 とタイムアウトが出ていた）。LLM に判定させないなら `gh variable set MAINT_ASSESSOR --body heuristic` で無駄な呼び出しを止める。
   - 逆に LLM に判定させるなら、`MAINT_ASSESSOR` が `heuristic` になっていないこと（無いか `llm`）を確かめる。`heuristic` のままだと、Dependabot 側に鍵を入れてもヒューリスティックで判定する。
5. Ollama にするなら、Actions の runner から届くホストの `http://<host>:11434/v1` と `llama3.1:8b`、`MAINT_LLM_API_KEY` は任意の値。

## 元に戻す

1 で控えた値を `gh variable set MAINT_LLM_BASE_URL` / `gh variable set MAINT_LLM_MODEL` で戻し、`gh secret set MAINT_LLM_API_KEY` に元の鍵を入れ直す。Dependabot 側に入れたなら `gh secret set MAINT_LLM_API_KEY --app dependabot` も戻すか `gh secret delete MAINT_LLM_API_KEY --app dependabot` で消す。`MAINT_ASSESSOR` は 1 で控えた状態に戻す（控えた値があれば `gh variable set MAINT_ASSESSOR --body "<控えた値>"`、無しなら `gh variable delete MAINT_ASSESSOR`）。

## 結果

保守の定期実行のモデルは、Amazon Bedrock の Claude Haiku 4.5（`MAINT_LLM_MODEL=bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0`、US の inference profile、IAM（GitHub の OIDC）で呼ぶ）。2026-10-09 に切り替えた。
経緯（PBI #348 の決定ログ）:
- 最初は Bedrock の GPT-6 luna（`bedrock/global.openai.gpt-6-luna`）に決めていた。手動実行が Gemini の利用枠切れ（429）で確かめられなかったため、利用者の指示で決めた（PBI #157 の決定ログ 2026-09-26、コードは PR #384）
- 2026-10-08、GPT-6 luna はこのアカウントで呼べなかった（`AccessDeniedException`「not available for this account」）。利用者は Claude Haiku 5.5 を選んだ
- 2026-10-09、Anthropic の利用目的の申請のあと Haiku 4.5 は呼べたが、Haiku 5.5 は GPT-6 luna と同じ理由で呼べなかった。利用者の判断で、呼べる Haiku 4.5 にした。Haiku 5.5 が使えるようになったら、`MAINT_LLM_MODEL` と IAM ロールの権限を替える

切り替えの準備は `docs/maintenance/README.md` の「Amazon Bedrock（IAM。API キー不要）」。IAM ロールの権限は、この inference profile と、それを通したときだけの基のモデル（`anthropic.claude-haiku-4-5-20251001-v1:0`）への `bedrock:InvokeModel`。
切り替えたあと、tech-watch の手動実行（run 37941853160）が通り、提案 2 件が起票された（#611、#612）。
Bedrock 上の価格は未確認（上の表に Claude のモデルは無い）。

上の候補を `dak-maint compare-models` で同じ入力で比べる実行はしていない。モデルが決まっているので不要と利用者が判断した（PBI #348 の決定ログ 2026-10-07）。
`compare-models` は、また別のモデルを検討するときに使える。
