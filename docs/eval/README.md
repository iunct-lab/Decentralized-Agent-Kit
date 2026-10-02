# Eval スコアボード（継続テスト）

DAK の「実モデル」品質を継続的に測るための仕組み（要件6）。

## 2 つのテスト層

1. **決定論 golden replay**（PR CI, 高速, 実LLM不要）
   `tests/integration/golden/*.json` を fake-LLM で再生し、ツール呼び出しの回帰を検出。
   → `tests/integration/test_golden_replay.py`
   → 増やし方: `tests/integration/golden/README.md`

2. **nightly 実LLM eval**（夜間, 小型 Ollama, 寛容アサーション）
   `.github/workflows/nightly-eval.yml` が小型モデルでゲート済みスモークを実行し、
   pass-rate を `history.jsonl` に追記する。PR ゲートではなく傾向シグナル。

補助: 同じスモークを安価なクラウドモデルで手動実行することもできる
（`./scripts/smoke_cloud_llm.sh`、既定 `openai/gpt-5.6-luna`、1 回数セント程度）。
ローカルの GPU/メモリが塞がっている時や、プロバイダ採用前の品質確認に使う。
Bedrock のクラウドモデルで回すときの費用・認証（GitHub OIDC）・頻度の比較は `cloud-eval.md`。

## history.jsonl

1 行 1 実行の JSON Lines:

```json
{"date": "2026-07-04", "model": "llama3.2:3b", "total": 4, "passed": 3, "failed": 1, "skipped": 0, "pass_rate": 0.75}
```

pass_rate の推移を見て、モデル更新やプロンプト改善の効果を追跡する。

## 「使うほど育つ」ループ

```
実LLMスモークで新しい成功セッション
  └─ capture_golden.py で決定論 golden に凍結
       └─ capture-golden ワークフローが PR 提案
            └─ マージで PR CI の回帰スイートが増える
```
