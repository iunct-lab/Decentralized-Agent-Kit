"""nightly-eval の結果の記録（リリース eval-history の asset history.jsonl）と、経路ごとの月の実行回数の上限。

`provider` は経路（ollama / bedrock …）、`runner` は実行環境。`provider` の無い古い行は ollama とみなす。
"""

from __future__ import annotations

import datetime
import json
import os
import xml.etree.ElementTree as ET


def summarize_junit(path: str) -> dict:
    total = failures = errors = skipped = 0
    if os.path.exists(path):
        root = ET.parse(path).getroot()
        for s in root.findall("testsuite") or [root]:
            total += int(s.get("tests", 0))
            failures += int(s.get("failures", 0))
            errors += int(s.get("errors", 0))
            skipped += int(s.get("skipped", 0))
    passed = total - failures - errors - skipped
    return {
        "total": total, "passed": passed, "failed": failures + errors, "skipped": skipped,
        "pass_rate": round(passed / total, 3) if total else 0.0,
    }


def make_record(summary: dict, *, model: str, provider: str, runner: str, today: datetime.date) -> dict:
    return {"date": today.isoformat(), "model": model, **summary, "provider": provider, "runner": runner}


def count_runs(history_path: str, *, provider: str, year_month: str) -> int:
    if not os.path.exists(history_path):
        return 0
    with open(history_path, encoding="utf-8") as f:
        rows = [json.loads(line) for line in f if line.strip()]
    return sum(
        1 for r in rows
        if r.get("provider", "ollama") == provider and str(r.get("date", "")).startswith(year_month + "-")
    )


def check_budget(count: int, limit: int, *, provider: str) -> tuple[bool, str]:
    if count >= limit:
        return False, f"今月の {provider} の実行は {count} 回で、上限 {limit} 回に達した"
    return True, f"今月の {provider} の実行は {count} 回（上限 {limit} 回）"
