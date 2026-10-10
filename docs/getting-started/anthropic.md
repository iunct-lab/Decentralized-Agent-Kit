# Switchboard: Anthropic Haiku 5.5

モデルは `anthropic/claude-haiku-5-5`（LiteLLM と定期保守の選択形式）。
Anthropic API へ送るモデル ID は `claude-haiku-5-5`。
キーは `.env`、一時ファイル、Docker の環境変数設定に保存しない。

## ホストの Parameter Store

東京リージョン (`ap-northeast-1`) の `/switchboard/anthropic-api-key` に、
AWS コンソールから `SecureString` として登録する。Claude Code の
`/switchboard/claude-oauth-token` は別の認証情報なので流用しない。

Switchboard の `aws/template.yaml` のホスト読取ポリシーは、パラメーター名を
個別に列挙する。権限境界の `/switchboard/*` だけでは読取権限にならない。
ホストの読取ポリシーへ次のリソースを追加し、Switchboard のインフラ運用から
反映する（AWS 管理の KMS 鍵を使う前提）。

```yaml
- !Sub 'arn:aws:ssm:${AWS::Region}:${AWS::AccountId}:parameter/switchboard/anthropic-api-key'
```

必要な操作は `ssm:GetParameter`。独自の KMS 鍵で暗号化した場合は、その鍵への
復号権限も必要。Docker の実行プロセスがインスタンスロールへアクセスできる
構成を使い、AWS のアクセスキーをファイルやコンテナ設定へコピーしない。

```bash
docker compose -f docker-compose.yml -f docker-compose.anthropic.yml up -d --build agent
```

Compose に入るのはモデル名とパラメーター名だけ。`agent/entrypoint.sh` が
コンテナ内部で `ssm_exec.py` を実行し、取得した値をサーバープロセスの環境へ
渡す。Docker の作成時の設定には API キーを含めない。SSM のアクセス拒否・
通信エラー・空の値・SecureString 以外は起動失敗となり、自動再試行しない。
既存の `.env` は非秘密の設定だけに使う。

## GitHub の定期保守

利用者が明示的に登録した Actions secret `MAINT_LLM_API_KEY` を使う。
コードが main に反映されてから、次の既定値へ変更する。

```bash
gh variable set MAINT_LLM_MODEL --body anthropic/claude-haiku-5-5
```

`MAINT_LLM_BASE_URL` は設定しない。`MAINT_AWS_ROLE_ARN` を削除すると
定期保守の Bedrock 用 OIDC 認証手順が実行されなくなる。IAM ロール自体の削除は不要。
`dependency-triage` はキーと Anthropic モデルがある場合に LLM 評価を選択する。
`nightly-eval` と `capture-golden` のローカル Ollama 評価は別の設定。

定期保守は native Messages API を使い、Haiku 5.5 が拒否する `temperature=0`
などの sampling パラメーターを送らない。thinking は本文と分け、拒否・打ち切り・
本文のない応答は失敗として扱う。HTTP の再送は行わない。

公式: [Haiku 5.5 移行ガイド](https://platform.claude.com/docs/en/models/haiku-5-5/migration-guide)。

設定後の最小通信確認は `gh workflow run verify-anthropic.yml`。既存の Actions secret を
そのステップのプロセス環境だけで使用し、短い確認を 1 回送る。キーや応答本文はログに
出さず、提案 Issue は起票しない。実通信には API 利用量が発生する。
