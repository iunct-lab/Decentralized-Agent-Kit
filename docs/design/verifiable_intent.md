# Verifiable Intent: 利用者が署名した支払いの意図で送金を止めるか

PBI #301（元要望 #44・#48・#54）。利用者が自分の鍵で署名した「1 回いくらまで」「この宛先にだけ」という意図（SD-JWT）を、
`send_sol_payment` の実行前に検証し、範囲外の送金を理由付きで止める形が DAK に合うかを判断するための文書。
検証する範囲・使うライブラリ・DAK の「AP2」との呼び分けを決め、採否は利用者が決める（「決定」節）。コードは変えない。

## 仕様の要約（2026-10-02 に一次資料で確認）

### Verifiable Intent（VI）v0.1

- 版: 規範の文書（`README.md`・`constraints.md`・`credential-format.md`・`security-model.md`）の見出しは `Version: 0.1-draft`、`Status: Draft`、
  `Date: 2026-02-18`（`design-rationale.md` は参考の文書で、版の見出しが無い）。リポジトリのタグ `v0.1.0`、ライセンス Apache-2.0
  （GitHub API のリポジトリ情報と、タグの `pyproject.toml` の `license`）。仕様の文書の本文にはライセンスの記載が無い
- 3 層の SD-JWT の委譲チェーン（`spec/credential-format.md` §2〜§5）:

| 層 | 署名する人 | 中身 | 寿命（推奨） |
|---|---|---|---|
| L1 | 発行者（銀行・ネットワーク） | 利用者の身元と、利用者の公開鍵（`cnf.jwk`） | 1 年以内 |
| L2 | 利用者（L1 の `cnf.jwk` の鍵） | 支払いの意図。即時モードは確定値（`typ: kb-sd-jwt`）、自律モードは制約とエージェントの公開鍵（`typ: kb-sd-jwt+kb`、`cnf.jwk` / `cnf.kid`） | 即時 15 分以内、自律 24 時間〜30 日（既定 30 日、L1 の `exp` を超えない） |
| L3a / L3b | エージェント（L2 の `cnf.jwk` の鍵） | 実際の支払い（`payment_amount`、`payee` ほか）と購入内容。自律モードだけ | 5 分（最大 1 時間） |

- L2 の本体（`credential-format.md` §4.3、§9.1）: 常に見える `nonce`・`aud`・`iat`・`exp`・`sd_hash`・`delegate_payload`・`_sd_alg`・`_sd`。
  `delegate_payload` は mandate の disclosure への参照（`{"...": "<hash>"}`）の配列。支払いの mandate は disclosure の 1 つで、
  `{"vct": "mandate.payment.open", "cnf": ..., "payment_instrument": ..., "constraints": [...]}` の形（§4.5.2）。`constraints` は 1 つ以上
- L2 と L1 の結び付け: `sd_hash = B64U(SHA-256(ASCII(L1 の提示文字列)))`、かつ L1 の `cnf.jwk` の鍵で署名（§6.1）
- 署名は **ES256 だけ**。`alg` は ES256 以外を拒否し、ヘッダの値に従って方式を選ばない（`spec/security-model.md` §4.5、§5.1）。`_sd_alg` は `sha-256`。
  直列化は RFC 9901 の「JWT + `~` + disclosure… + 末尾の `~`」
- 時刻: `exp` は時計のずれ 300 秒まで許す。`iat` が「今 + 300 秒」より先なら拒否（`credential-format.md` §3.5、§4.7）。
  高い安全性が要る配置では 60 秒程度に詰めることを推奨（`security-model.md` §4.6）
- 登録済みの制約（`spec/constraints.md` §4、§6.2）。検証器はすべてに対応しなければならない（MUST）:

| `type` | 形 | DAK での扱い（案） |
|---|---|---|
| `payment.amount` | `{currency, min?, max?}`。`currency` は ISO 4217、`min` / `max` は最小単位の整数（`10000` = $100.00）。片方が無ければその側は無制限 | **実施** |
| `payment.allowed_payee` | `{allowed_payees: [{id?, name, website}]}`。照合は `id` があれば `id`、無ければ `name` と `website` | **実施**（`id` の完全一致だけ。下の「仕様との差」） |
| `payment.budget` | `{currency, max}`。累計の上限。累計はネットワークが数える | 拒否（未対応） |
| `payment.recurrence` / `payment.agent_recurrence` | 頻度・開始日・終了日・回数 | 拒否（未対応） |
| `payment.reference` | `{conditional_transaction_id}`（checkout の mandate との結び付け） | 拒否（未対応） |
| `mandate.checkout.allowed_merchant` / `mandate.checkout.line_items` | 加盟店・品目の allowlist | 拒否（未対応。DAK に checkout が無い） |

- **自律モードの支払いの mandate には `payment.reference` が必須**: 「Autonomous payment mandate MUST include a `payment.reference` constraint」
  （`credential-format.md` §4.5.3）。checkout の mandate（`mandate.checkout.open`）と対にし、対の無い mandate は拒否する（同 §8.2）。
  つまり VI に準拠した自律モードの L2 は、必ず checkout の mandate と `payment.reference` を含む
- **選択的開示**: `payment.amount` などは制約ごと開示するか隠すか（`constraints.md` §6.2 の「property」）。`allowed_payees` の各項目は入れ子の
  disclosure（`{"...": "<hash>"}`）にできる。仕様では、検証者に開示されなかった項目は失敗にならず、開示された項目が無ければその制約の判定を飛ばす（§4.3 の手順 3 と注）
- **未知の制約**: 「strictness のモードにかかわらず、未知の制約を含む open な mandate は拒否しなければならない」（`constraints.md` §5.4）。
  独自の制約の名前は URN か逆ドメイン（§6.1）
- AP2・ACP・UCP との対応は「専用の統合ガイドに書く」とあり（`spec/README.md` §9.3）、仕様本体には無い。統合ガイドの中身は**未確認**
- 参照実装: `src/verifiable_intent/`（発行・検証・制約の判定）。依存は `cryptography>=42.0` だけ、`Development Status :: 3 - Alpha`。
  PyPI には未公開（`https://pypi.org/pypi/verifiable-intent/json` が 404。2026-10-02 確認）

### Google の AP2（Agent Payments Protocol）v0.2

- 版: `docs/ap2/specification.md` の題は「Agentic Payment Protocol (v0.2)」。リリース `v0.2.0`（2026-04-28）、リポジトリのライセンス Apache-2.0
- Mandate は SD-JWT の VC（`specification.md`「Verifiable Digital Credential Formats」）。`vct` は `mandate.payment.1`・`mandate.payment.open.1`・
  `mandate.checkout.1`・`mandate.checkout.open.1` で、完全一致で照合する（同「Mandate Versioning」）
- 自律モードでは、利用者が制約とエージェントの公開鍵（`cnf`）を入れた open な mandate に署名し、エージェントが確定した（closed な）mandate に署名する
  （`specification.md`「Modes」、`agent_authorization.md`「Mandate Structure」）。VI の L2 / L3 と同じ構造
- 制約の名前が VI と違う: `payment.amount_range`（`{currency, min, max}`）、`payment.allowed_payees`（`{allowed: [{id?, name, website}]}`）、
  `payment.budget`、`payment.execution_date`、`payment.allowed_payment_instruments`、`payment.allowed_pisps` ほか（`payment_mandate.md`）
- 金額の単位が文書の中で揃っていない: 制約の節の例は小数（`"max": 100.50`）、SD-JWT の例は整数（`"max": 20000`）。スキーマはテンプレートの
  プレースホルダで、文書からは読めない。最小単位か ISO 4217 かの記載は無い
- 未知の制約は「評価に失敗したものとして扱わなければならない」（`agent_authorization.md`「Verification and Processing Rules」）。VI と同じ
- AP2 の文書に Verifiable Intent への言及は無い

### 出典

| 資料 | 版・日付 |
|---|---|
| https://github.com/agent-intent/verifiable-intent の `spec/README.md`・`constraints.md`・`credential-format.md`・`security-model.md`・`design-rationale.md`、`pyproject.toml` | タグ `v0.1.0`。2026-10-02 確認 |
| https://verifiableintent.dev | 2026-09-24 確認（PBI #301 の背景） |
| https://github.com/google-agentic-commerce/AP2 の `docs/ap2/specification.md`・`payment_mandate.md`・`checkout_mandate.md`・`agent_authorization.md` | タグ `v0.2.0`（2026-04-28）。2026-10-02 確認 |
| https://pypi.org/pypi/verifiable-intent/json | `curl` の応答が 404。2026-10-02 確認 |
| GitHub API（`repos/agent-intent/verifiable-intent`、`repos/google-agentic-commerce/AP2` と `/releases`）、`verifiable-intent` のタグ `v0.1.0` の `pyproject.toml` | ライセンス・リリース日・依存・`Development Status`。2026-10-02 確認 |

## DAK の「AP2」と Google の AP2

名前が同じで中身が違う。この文書と #301 の実装では、DAK の流れを「DAK の支払いの流れ」、Google のものを「Google AP2」と書き、裸の「AP2」は使わない。

| | DAK の支払いの流れ（`AGENTS.md` §5 の「AP2 Protocol (Agent-to-Agent Payment Protocol)」） | Google AP2（Agent Payments Protocol） |
|---|---|---|
| 何か | DAK 独自の実装の流れ。仕様書は無い | 公開の仕様（v0.2） |
| 流れ | 有料ツールが `PaymentRequiredError`（`agent/dak_agent/errors.py`）を出す → Observation になる → エージェントが `send_sol_payment` で SOL を送る → 送金のハッシュを付けて再試行 | 利用者が Checkout / Payment Mandate（SD-JWT）に署名し、エージェント・加盟店・決済ネットワークがそれを検証する |
| 認可の証明 | 無い（送金のハッシュは「払った」証明で、「払ってよい」証明ではない） | Mandate の署名チェーン |
| #301 との関係 | 検証を差し込む先（`send_sol_payment` の前段） | 制約の考え方は同じ。名前は VI に合わせる（下の「決定」で確認） |

#301 は Google AP2 にも VI にも「準拠」しない。VI の L2 の形を借りて、DAK の支払いの流れの送金の前に検証を 1 つ足すだけ。

## DAK で検証する範囲（案）

| 項目 | 案 | 理由 |
|---|---|---|
| 層 | **L2 だけ**。L1 と L3 は検証しない | L1 は発行者（銀行など）が要り、DAK には居ない。L3 はエージェントが自分の鍵で署名する層で、署名する主体も検証する相手（決済ネットワーク）も DAK の中にいる。今の目的（範囲外の送金を止める）には L2 の制約で足りる。広げるなら別 PBI |
| 信頼の起点 | 環境変数 `DAK_PAYMENT_INTENT_TRUSTED_JWKS`（利用者の**公開鍵**の JWKS）。秘密鍵はリポジトリにも env の例にも置かない | L1 の `cnf.jwk` の代わり。配置する人が利用者の公開鍵を明示的に信頼する |
| モード | 自律モードの L2（`vct: mandate.payment.open`）。`cnf`（エージェントの鍵）は読まない | DAK ではエージェントが金額と宛先を決めるので、確定値の即時モードより制約の自律モードが合う。L3 を使わないので `cnf` は使い道が無い |
| 署名 | ヘッダの `alg` が `ES256` でなければ拒否し、検証も ES256（P-256）に固定する（ヘッダの値で方式を選ばない） | 仕様どおり（`security-model.md` §4.5、§5.1） |
| disclosure | `_sd_alg` は `sha-256` だけ。`delegate_payload` から入れ子（`allowed_payees` の項目など）までたどり、すべての `{"...": "<hash>"}` に SHA-256 が一致する disclosure があること。解けない参照が 1 つでも残れば拒否。どの参照からも使われない disclosure も拒否 | 改ざんと隠蔽の検出。入れ子の disclosure を差し替えても外側の署名とハッシュは変わらないので、入れ子まで照合しないと宛先を差し替えられる。隠した制約を「無いもの」として通すと、金額の上限を隠した意図で上限を超えられる |
| 開示 | 意図はすべての disclosure を付けて渡す（DAK の検証者は利用者の意図の全体を見る）。上の「解けない参照は拒否」により、隠した制約・宛先は通らない | 仕様は開示されなかった宛先を失敗にしないが、DAK では検証者が 1 人なので隠す理由が無い |
| 時刻 | `exp` と `iat` を時計のずれ 300 秒で判定。寿命の上限（30 日）は強制しない | 仕様の推奨値。上限は SHOULD なので、まずは期限切れだけ止める |
| 実施する制約 | `payment.amount`、`payment.allowed_payee` | 元要望（#48「金額上限、宛先制限など」）の 2 つ |
| それ以外の制約 | すべて拒否（理由「未対応の制約」） | 仕様の MUST（未知の制約の拒否）を、未対応の登録済みの制約にも広げる。止めずに通すと、利用者が付けた制約が黙って無視される |

### 仕様との差（意図して外すところ）

- **通貨 `SOL`**: VI の `currency` は ISO 4217 で、暗号資産の扱いは書かれていない。DAK では通貨コード `SOL`、最小単位を lamports（1 SOL = 10^9 lamports）とし、
  `min` / `max` は lamports の整数にする。`send_sol_payment` の `amount`（SOL の小数）は lamports に変換して比べる（丸めの方針は #303 で docstring に書く）
- **宛先の照合は `id` だけ**: 仕様は `id` が無い項目を `name` と `website` で照合するが、SOL の送金先は base58 のアドレスしか無いので、
  `id`（アドレス）の完全一致だけを見る。`id` の無い項目は一致しないものとして扱い、項目が空なら拒否する
- **L1 との結び付け（`sd_hash`）を見ない**: L1 を扱わないため。信頼は上の JWKS で置き換える
- **checkout の mandate と `payment.reference` を要求しない（DAK のプロファイル）**: DAK には checkout が無く、L3 も使わないので、対にする相手が無い。
  DAK が受け付けるのは「支払いの mandate 1 つだけの L2」で、`payment.reference` を含む意図は「未対応の制約」で拒否する。
  そのため **VI に準拠した自律モードの意図（必ず `payment.reference` を含む）はそのままでは通らない**。VI の発行のツールで作った意図を使い回せないのが、この案の相互運用の代償
- **開示の扱いが仕様より厳しい**: 解けない disclosure の参照を拒否する（上の「開示」）。仕様は開示されなかった宛先を失敗にしない
- **登録済みの制約にも対応しないものがある**: 仕様は「すべてに対応しなければならない」。DAK は対応しないものを拒否するので、安全側にずれる（通る送金が減るだけ）

## ライブラリ: 参照実装か、自前か

| 観点 | (a) 参照実装（`verifiable-intent` を git のタグ `v0.1.0` に固定して入れる） | (b) 自前の最小検証器（`cryptography` だけ） |
|---|---|---|
| 依存の追加 | パッケージ 1 つ（git 依存）。その依存は `cryptography>=42.0` だけ | 無し。`cryptography` 46.0.3 は agent のロックファイルに推移的な依存として既にある。#303 で直接の依存として宣言する |
| 入れ方 | PyPI に無いので `uv` の git 依存。lock は commit の SHA で固定される | 今の PyPI の依存のまま |
| safe-chain（依存の最低経過日数のゲート） | git 依存にゲートが掛かるかは**未確認**（PyPI の公開日で判定する仕組みなら素通りする。ゲートの外で入ることになる） | 関係しない |
| 範囲 | 発行・L1〜L3・全制約。DAK が使わない部分が大半 | L2 の署名・disclosure・時刻・2 つの制約だけ（数百行以内の見込み） |
| 仕様の変化への追従 | 版を上げれば追従できる。ただし Draft で Alpha なので API が変わりうる | 自分で追う。範囲が狭いので影響は小さい |
| DAK の差（`SOL`、`id` だけの照合）との相性 | 通貨を ISO 4217 前提で検証していれば合わない（コードは読み切っていない。**未確認**） | 差をそのまま書ける |
| 保守 | Mastercard の保守に依存。PyPI 公開まで git 依存が続く | DAK が持つ。テストで固める |

**推奨: (b) 自前**。DAK が使うのは L2 の検証のごく一部で、依存のゲートの外から未公開のパッケージを入れる利点が小さい。`joserfc` / `pyjwt` も lock にあるが、
SD-JWT の disclosure の扱いは自分で書くことになり、`alg` を固定するなら JWS の検証も `cryptography` の ECDSA で直接書いた方が短い。
参照実装は、その発行のコードで作った SD-JWT をテストの入力に使えるか #303 で確かめる（使えればテストに出典を書く。使えなければ鍵を実行時に作って自分で発行する）。

## 送金を止める場所（#304 への申し送り）

PBI #301 の決定ログどおり、ADK の `before_tool_callback` を `AdaptiveAgent` に渡す。この PBI を書いた後に、ツールの前段のプラグイン
（`PermissionPlugin`、`agent/dak_agent/permission.py`。`ContextHarnessPlugin`、`agent/dak_agent/harness.py`）が入った。ADK はプラグインの
コールバックをエージェントのコールバックより先に呼ぶ見込みなので（#304 で確かめる）、確認を待つ・拒否するプラグインの判定が先、意図の検証が後になる。
#304 はこの順番を確かめてテストとコードのコメントに書く。置き場所を変える必要が出たら、#304 と PBI の決定ログに書いてから変える。
意図の検証は「止めるだけ」で、承認を代わりに出さない。

## 採用するなら

#303（検証器）→ #304（`send_sol_payment` の前段）→ #305（統合テストと設定の文書）の順。既定は無効（`ENABLE_PAYMENT_INTENT_CHECK=false`）で、
今の支払いの流れは変わらない。制約の名前は下の「決定」に従って #301・#303 の本文を直す。

## 採用しないなら

#303〜#305 を閉じ、#301 を不採用として閉じる。この文書は判断の記録として残す。`send_sol_payment` の docstring の「有効な Intent Mandate に基づくときだけ」は
コードで確かめていない、という事実は残る。

## 決定

（利用者の回答を待っている。PBI #301 の決定ログに引用する）
