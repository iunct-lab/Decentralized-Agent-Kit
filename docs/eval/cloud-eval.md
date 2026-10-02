# クラウドモデルでの実LLMスモーク（Bedrock）を CI に入れるかの材料

夜間の Ollama（`nightly-eval.yml`）と同じスモーク（`tests/integration/test_smoke_real_llm.py` の 4 件）を、
Bedrock 上のクラウドモデルで手動実行するときの費用・認証・頻度を比べる。
決めること（使うか・認証・費用の上限）は元要望 #68、計画は PBI #334。

この文書の数字は見積もり。実際に 1 回回した結果と費用は、実行後に「結果」の節に書く。

## 1 回あたりのトークン数（Ollama での実測）

無料のランナーで回った nightly-eval（`llama3.1:8b`、2026-10-01、run 36846026870、4/4 pass）の
成果物 `ollama.log` から、llama.cpp が出すリクエストごとの値を拾った。
入力は `new prompt … task.n_tokens`（キャッシュ分も含むプロンプト全体）、出力は `eval time … / N tokens`。

| テスト | LLM 呼び出し | 入力トークン | 出力トークン |
|---|---:|---:|---:|
| `test_basic_chat_responds` | 2 | 1,143 | 35 |
| `test_skill_discovery_and_use` | 5 | 8,331 | 260 |
| `test_enforcer_forces_tool_usage` | 1 | 1,458 | 41 |
| `test_ap2_payment_flow_with_real_llm` | 8 | 8,092 | 327 |
| **合計** | **16** | **19,024** | **663** |

目安であることに注意:

- トークン化はモデルごとに違う。同じ文でも Bedrock のモデルでは数が変わる
- 呼び出しの回数はモデルの振る舞いで変わる（ツールを何回呼ぶか、言い直すか）
- 下の費用は、この値をそのまま掛けたもの（倍率なし）と、余裕を見た 2 倍の両方を書く

## 候補モデル

確認日はすべて 2026-10-02。価格は 1M トークンあたりの USD（Standard、短いコンテキスト）。
1 回あたりの費用は、上の合計（入力 19,024 / 出力 663）を掛けたもの。

| モデル | LiteLLM のモデル名の候補 | 入力 / 出力 | 1 回あたり | 出典 |
|---|---|---|---:|---|
| GPT-5.6 Luna（Bedrock、Geo CRIS `us.`） | `bedrock/us.openai.gpt-5.6-luna`、通らなければ `bedrock/converse/us.openai.gpt-5.6-luna` | $0.22 / $1.32 | $0.0051 | [モデルカード](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-56-luna.html)（2026-10-02 確認） |
| GPT-5.6 Luna（Bedrock、Global CRIS） | `bedrock/global.openai.gpt-5.6-luna`（同上） | $0.20 / $1.20 | $0.0046 | 同上（2026-10-02 確認） |
| Claude Haiku 4.5（Bedrock） | `bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0` | 未確認（二次資料では $1.00 / $5.00） | 未確認（二次資料の値なら $0.022） | [モデルカード](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-haiku-4-5.html)は価格を [Bedrock の価格ページ](https://aws.amazon.com/bedrock/pricing/) に送るが、そのページで値を確かめられなかった（2026-10-02） |
| Nova Micro（Bedrock） | `bedrock/us.amazon.nova-micro-v1:0` | 未確認（二次資料では $0.035 / $0.14） | 未確認（二次資料の値なら $0.0008） | [モデルカード](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-amazon-nova-micro.html)に価格は無く、[価格ページ](https://aws.amazon.com/bedrock/pricing/)でも確かめられなかった（2026-10-02） |
| GPT-5.6 Luna（OpenAI 直、比較用） | `openai/gpt-5.6-luna` | 未確認（$0.20 / $1.20 と推定） | 未確認（推定の値なら $0.0046） | Bedrock のモデルカードの「In-Region は OpenAI の価格に 10% を足したもの」から逆算。OpenAI の価格ページは開いていない（2026-10-02） |

モデルごとの注意（いずれもモデルカード、2026-10-02 確認）:

- **GPT-5.6 Luna**: bedrock-runtime で使える API は Responses・Chat Completions・Converse。Invoke は使えない。
  In-Region の推論は無く、`us.` か `global.` の推論プロファイルを名指しする。
  IAM には推論プロファイルに加えて、既定プロジェクト（`arn:aws:bedrock:{region}:{account-id}:project/default`）への `bedrock:InvokeModel` が要る。
  提供終了は「2027-07-13 より前ではない」
- **Claude Haiku 4.5**: 「EOL no sooner than: Oct 16, 2026」「Model EOL date: No sooner than 10/1/2026」。
  2026-10 以降、いつ提供終了の案内が出てもおかしくない。費用は Bedrock ではなく AWS Marketplace のモデル提供者の項目として請求に出る
- **Nova Micro**: 推論プロファイルは `us.` / `eu.` だけで、Global は無い。Converse・Invoke に対応、ツール呼び出しはクライアント側のみ

既定のモデルは `bedrock/us.openai.gpt-5.6-luna` にする。価格が一次資料で確かめられ、提供終了が近くないため（PBI #334 の決定ログ）。
LiteLLM でこの名前の形が通るかは未検証で、1 回目の実行で確かめる。

## 認証方式

| 方式 | GitHub に置くもの | 有効期限 | 向き不向き |
|---|---|---|---|
| **GitHub OIDC → STS AssumeRole**（SigV4） | ロールの ARN とリージョン（変数。秘密ではない） | ジョブごとに発行。`role-duration-seconds`（既定 3600 秒）とロールの最大セッション時間（既定 1 時間）の短い方 | CI 向き。長期の秘密が無く、信頼ポリシーでリポジトリと environment に絞れる。`aws-actions/configure-aws-credentials`（最新 v6.3.0、2026-09-15）が `AWS_ACCESS_KEY_ID` などを環境変数に入れ、コンテナへは `docker-compose.cloud-llm.yml` が渡す |
| **Bedrock API キー（長期）** | `AWS_BEARER_TOKEN_BEDROCK`（secret） | 作成時に指定（無期限も可） | 公式は「探索用に限る」ことを強く推奨し、本番では短期の認証情報に切り替えるよう書いている。漏れたときの影響が大きく、CI には向かない |
| **Bedrock API キー（短期）** | 毎回発行し直す必要がある | 最長 12 時間 | 発行に AWS の認証情報が要り、それを CI でどう得るかの問題が残る。結局 OIDC が要る |

出典: [Bedrock API キー](https://docs.aws.amazon.com/bedrock/latest/userguide/api-keys-how.html)（2026-09-24 確認）。

**OIDC を使う**。セッション時間とタイムアウトの関係: Bedrock で回すジョブは `timeout-minutes: 50` にする予定（#337）。
Ollama の CPU 推論では 4 件で約 25 分かかったが、クラウドモデルは 1 回の応答が数秒なので、1 時間のセッションで足りる。
ロールの最大セッション時間を延ばす必要はない。

### IAM の雛形

`<ACCOUNT_ID>` と `<REGION>` は利用者が埋める。作るのは利用者（PBI #334 のスコープ外）。

信頼ポリシー（GitHub の environment `cloud-eval` からのジョブだけが引き受けられる）:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::<ACCOUNT_ID>:oidc-provider/token.actions.githubusercontent.com"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
          "token.actions.githubusercontent.com:sub": "repo:iunct-lab/Decentralized-Agent-Kit:environment:cloud-eval"
        }
      }
    }
  ]
}
```

権限ポリシー（候補のモデルを呼ぶことだけ）。推論プロファイルは呼び出し元のリージョンに、基盤モデルは振り分け先のどのリージョンにもありうるので、基盤モデルはリージョンを `*` にする:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "InvokeCandidateModels",
      "Effect": "Allow",
      "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
      "Resource": [
        "arn:aws:bedrock:<REGION>:<ACCOUNT_ID>:inference-profile/us.openai.gpt-5.6-luna",
        "arn:aws:bedrock:<REGION>:<ACCOUNT_ID>:inference-profile/global.openai.gpt-5.6-luna",
        "arn:aws:bedrock:*::foundation-model/openai.gpt-5.6-luna",
        "arn:aws:bedrock:<REGION>:<ACCOUNT_ID>:inference-profile/us.anthropic.claude-haiku-4-5-20251001-v1:0",
        "arn:aws:bedrock:*::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0",
        "arn:aws:bedrock:<REGION>:<ACCOUNT_ID>:inference-profile/us.amazon.nova-micro-v1:0",
        "arn:aws:bedrock:*::foundation-model/amazon.nova-micro-v1:0"
      ]
    },
    {
      "Sid": "DefaultProjectForLuna",
      "Effect": "Allow",
      "Action": "bedrock:InvokeModel",
      "Resource": "arn:aws:bedrock:<REGION>:<ACCOUNT_ID>:project/default"
    }
  ]
}
```

使わないモデルの行は消してよい。Global の推論プロファイルを使うなら、その ARN の形はモデルカードと IAM の文書で確かめてから足す（未検証）。

## 実行頻度の案

1 回あたりの費用は上の表の値。「2 倍」はトークン数の見積もりの外れに備えた余裕。
GitHub Actions のランナーは、このリポジトリが公開なので無料。

| 頻度 | 月の回数 | Luna `us.`（1 回 $0.0051） | Luna `us.` の 2 倍 | Haiku 4.5（未確認。二次資料の値で $0.022） |
|---|---:|---:|---:|---:|
| 手動のみ（月の上限 4 回） | 最大 4 | $0.02 | $0.04 | $0.09 |
| 週 1 | 約 4.3 | $0.02 | $0.04 | $0.10 |
| 毎晩 | 約 30 | $0.15 | $0.31 | $0.67 |

どの案でも月 1 ドルを下回る見積もり。費用より、結果の読み方（傾向として何を見るか）と、
有料の経路を自動で回すこと自体を許すかで決めることになる。定期実行に入れるかは、1 回実行した結果を見て決める（PBI #334 の条件 6）。

## 結果

（1 回目の手動実行のあとに書く: 実行の URL、pass-rate、所要時間、通ったモデル名の形、実際の費用と見積もりとの差）
