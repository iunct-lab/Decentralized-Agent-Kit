import datetime
import json

import pytest

from dak_maintenance.cli import main
from dak_maintenance.eval_history import check_budget, count_runs, make_record, summarize_junit

JUNIT = """<?xml version="1.0" encoding="utf-8"?><testsuites name="pytest tests">
<testsuite name="pytest" errors="0" failures="1" skipped="0" tests="4">
<testcase name="a" /><testcase name="b" /><testcase name="c" /><testcase name="d"><failure /></testcase>
</testsuite></testsuites>"""


def _history(tmp_path, rows):
    path = tmp_path / "history.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def test_record_from_junit_keeps_old_keys_and_adds_provider(tmp_path):
    junit = tmp_path / "eval-report.xml"
    junit.write_text(JUNIT, encoding="utf-8")
    rec = make_record(summarize_junit(str(junit)), model="llama3.1:8b", provider="ollama",
                      runner="github-hosted", profile="", today=datetime.date(2026, 10, 2))
    assert rec == {
        "date": "2026-10-02", "model": "llama3.1:8b", "total": 4, "passed": 3, "failed": 1,
        "skipped": 0, "pass_rate": 0.75, "provider": "ollama", "runner": "github-hosted", "profile": "",
        "vulnerabilities": {},
    }
    assert list(rec)[:7] == ["date", "model", "total", "passed", "failed", "skipped", "pass_rate"]


def test_missing_junit_is_zero_tests(tmp_path):
    assert summarize_junit(str(tmp_path / "none.xml")) == {
        "total": 0, "passed": 0, "failed": 0, "skipped": 0, "pass_rate": 0.0,
    }


def test_count_treats_rows_without_provider_as_ollama_and_skips_other_months(tmp_path):
    path = _history(tmp_path, [
        {"date": "2026-09-30", "model": "llama3.1:8b"},                       # 前の月
        {"date": "2026-10-01", "model": "llama3.1:8b"},                       # provider なし = ollama
        {"date": "2026-10-02", "model": "x", "provider": "bedrock"},
        {"date": "2026-10-03", "model": "x", "provider": "bedrock"},
        {"date": "2026-11-01", "model": "x", "provider": "bedrock"},          # 次の月
    ])
    assert count_runs(str(path), provider="ollama", year_month="2026-10") == 1
    assert count_runs(str(path), provider="bedrock", year_month="2026-10") == 2
    assert count_runs(str(tmp_path / "none.jsonl"), provider="bedrock", year_month="2026-10") == 0


def test_budget_denies_at_the_limit():
    assert check_budget(3, 4, provider="bedrock")[0] is True
    allowed, reason = check_budget(4, 4, provider="bedrock")
    assert allowed is False
    assert reason == "今月の bedrock の実行は 4 回で、上限 4 回に達した"


def test_budget_cli_writes_allowed_false_and_reason(tmp_path, monkeypatch):
    month = datetime.date.today().strftime("%Y-%m")
    path = _history(tmp_path, [{"date": f"{month}-01", "model": "x", "provider": "bedrock"}])
    out, summary = tmp_path / "out", tmp_path / "summary"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert main(["eval-budget", "--history", str(path), "--provider", "bedrock", "--limit", "1"]) == 0
    assert "allowed=false\n" in out.read_text()
    assert "reason=今月の bedrock の実行は 1 回で、上限 1 回に達した\n" in out.read_text()
    assert "上限 1 回に達した" in summary.read_text()


def test_record_cli_appends_one_line(tmp_path, monkeypatch):
    junit = tmp_path / "eval-report.xml"
    junit.write_text(JUNIT, encoding="utf-8")
    path = _history(tmp_path, [{"date": "2026-09-24", "model": "llama3.1:8b", "pass_rate": 0.75}])
    out, summary = tmp_path / "out", tmp_path / "summary"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert main(["eval-record", "--junit", str(junit), "--history", str(path),
                 "--model", "bedrock/us.openai.gpt-5.6-luna", "--provider", "bedrock"]) == 0
    lines = path.read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1])["provider"] == "bedrock"
    assert json.loads(lines[1])["runner"] == "github-hosted"
    assert json.loads(lines[1])["profile"] == ""
    assert out.read_text() == "pass_rate=0.75\n"
    assert summary.read_text() == "### Nightly eval (bedrock/us.openai.gpt-5.6-luna)\n\n- pass_rate: **0.75** (3/4, skipped 0)\n"


def test_record_cli_writes_the_profile(tmp_path, monkeypatch):
    # プロファイルあり/なしの行を見比べるために、DAK_HARNESS_PROFILE に渡した名前を行に残す
    junit = tmp_path / "eval-report.xml"
    junit.write_text(JUNIT, encoding="utf-8")
    path = tmp_path / "history.jsonl"
    summary = tmp_path / "summary"
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert main(["eval-record", "--junit", str(junit), "--history", str(path),
                 "--model", "llama3.1:8b", "--profile", "qwen-small"]) == 0
    assert json.loads(path.read_text())["profile"] == "qwen-small"
    assert summary.read_text().startswith("### Nightly eval (llama3.1:8b, profile qwen-small)\n")


@pytest.mark.parametrize("argv", [
    ["eval-budget", "--provider", "bedrock", "--limit", "4"],
    ["eval-record", "--junit", "x.xml", "--model", "m"],
])
def test_history_path_is_required(argv):
    # 既定の相対パスにすると、maintenance/ から実行したとき別の履歴を数えて上限を素通りする
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2


def _record_argv(tmp_path, *extra):
    junit = tmp_path / "eval-report.xml"
    junit.write_text(JUNIT, encoding="utf-8")
    history = tmp_path / "history.jsonl"
    return ["eval-record", "--junit", str(junit), "--history", str(history), "--model", "m", *extra], history


def test_record_has_vulnerabilities_per_lock(tmp_path):
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()
    vuln = {"id": "GHSA-aaaa", "dependency": {"name": "p", "version": "1.0"}}
    (audit_dir / "agent.json").write_text(json.dumps({"vulnerabilities": [vuln]}))
    (audit_dir / "cli.json").write_text(json.dumps({"vulnerabilities": []}))
    (audit_dir / "bff.json").write_text("not json")
    argv, history = _record_argv(tmp_path, "--audit-dir", str(audit_dir))
    assert main(argv) == 0
    row = json.loads(history.read_text().splitlines()[-1])
    v = row["vulnerabilities"]
    assert v["agent"] == {"ids": ["GHSA-aaaa"], "error": None}
    assert v["cli"] == {"ids": [], "error": None}
    assert v["bff"]["ids"] == [] and v["bff"]["error"]


def test_record_without_audit_dir_has_empty_vulnerabilities(tmp_path):
    argv, history = _record_argv(tmp_path)
    assert main(argv) == 0
    assert json.loads(history.read_text().splitlines()[-1])["vulnerabilities"] == {}
