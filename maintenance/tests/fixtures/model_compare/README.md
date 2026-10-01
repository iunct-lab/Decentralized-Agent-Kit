`dak-maint compare-models` の固定入力。実行ごとに結果が変わらないよう、Web 検索（Tavily）と changelog の取得をしない。

- `charter.md`: `docs/CHARTER.md` の写し（2026-10-01）
- `search_results.json`: tech-watch / charter-review の検索結果の代わり。公開ページの題名・URL と、自分で書いた要約。最後の 1 件は憲章の範囲外（範囲外を外せるかを見る）
- `deps.json`: feature-sync の依存 2 件。changelog は各プロジェクトの GitHub Releases の抜粋（fastapi/fastapi 0.142.0、modelcontextprotocol/python-sdk v2.2.0。作者名は除いた）
- `changelog.txt`: triage のリスク評価の入力。encode/httpx 0.28.0 の Releases の抜粋（削除された引数を含む）
