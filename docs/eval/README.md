# Eval スコアボード（継続テスト）

DAK の「実モデル」品質を継続的に測るための仕組み（要件6）。

## 2 つのテスト層

1. **決定論 golden replay**（PR CI, 高速, 実LLM不要）
   `tests/integration/golden/*.json` を fake-LLM で再生し、ツール呼び出しの回帰を検出。
   → `tests/integration/test_golden_replay.py`
   → 増やし方: `tests/integration/golden/README.md`

2. **nightly 実LLM eval**（夜間, 小型 Ollama, 寛容アサーション）
   `.github/workflows/nightly-eval.yml` が小型モデルでゲート済みスモークを実行し、
   pass-rate を `history.jsonl`（リリース `eval-history` の asset）に追記する。PR ゲートではなく傾向シグナル。

補助: 同じスモークを安価なクラウドモデルで手動実行することもできる
（`./scripts/smoke_cloud_llm.sh`、既定 `openai/gpt-5.6-luna`、1 回数セント程度）。
ローカルの GPU/メモリが塞がっている時や、プロバイダ採用前の品質確認に使う。
Bedrock のクラウドモデルで回すときの費用・認証（GitHub OIDC）・頻度の比較は [cloud-eval.md](cloud-eval.md)。

## history.jsonl

記録は git の外に持つ: リリース `eval-history`（版ではない固定のリリース）の asset `history.jsonl`。
毎晩 nightly-eval が asset を落として 1 行足し、その全体の写しを実行の artifact `nightly-eval` に残してから asset を差し替える。
main への直接の push はルールセットに止められ、評価の結果は枝に含める内容でもないため（#517）。

```bash
gh release download eval-history -p history.jsonl          # 今の記録を落とす
```

1 行 1 実行の JSON Lines:

```json
{"date": "2026-10-02", "model": "llama3.1:8b", "total": 4, "passed": 3, "failed": 1, "skipped": 0, "pass_rate": 0.75, "provider": "ollama", "runner": "github-hosted"}
```

- `provider`: どの経路で回したか（`ollama` / `bedrock`）。この項目の無い古い行は `ollama` とみなす
- `runner`: 実行環境（GitHub のランナーなら `github-hosted`）

行は `dak-maint eval-record`（`maintenance/`）が JUnit XML から書く。経路ごとの今月の実行回数は
`dak-maint eval-budget --history <落とした history.jsonl> --provider <経路> --limit <回数>`（`maintenance/` で実行）が数え、上限に達していれば `allowed=false` と理由を出す。

差し替え（`gh release upload … --clobber`）は消してから上げるので、途中で失敗すると asset が消えることがある。
そのときは最後に成功した実行の artifact の写しから戻す:

```bash
gh run download <run-id> -n nightly-eval -D restore     # restore/eval-history/history.jsonl
gh release upload eval-history restore/eval-history/history.jsonl --clobber
```

pass_rate の推移を見て、モデル更新やプロンプト改善の効果を追跡する。

## 「使うほど育つ」ループ

```
実LLMスモークで新しい成功セッション
  └─ capture_golden.py で決定論 golden に凍結
       └─ capture-golden ワークフローが PR 提案
            └─ マージで PR CI の回帰スイートが増える
```
