import json
import subprocess
from pathlib import Path

from dak_maintenance import audit

FIX = Path(__file__).parent / "fixtures"


def test_parse_lists_ids():
    r = audit.parse_audit_json((FIX / "uv_audit_cli.json").read_text())
    assert r.error is None
    assert len(r.ids) == 3 and len(set(r.ids)) == 3
    assert all(k.count("==") == 1 for k in r.packages)
    assert sorted(i for ids in r.packages.values() for i in ids) == sorted(r.ids)


def test_parse_empty_has_no_ids():
    r = audit.parse_audit_json((FIX / "uv_audit_empty.json").read_text())
    assert r.ids == [] and r.error is None


def test_audit_failure_is_an_error_not_zero(monkeypatch):
    def fake(out, code):
        return lambda *a, **k: subprocess.CompletedProcess(a, code, stdout=out, stderr="boom")

    monkeypatch.setattr(audit.subprocess, "run", fake("not json", 2))
    r = audit.audit_lock(".")
    assert r.error and r.ids == []
    monkeypatch.setattr(audit.subprocess, "run", fake("", 1))
    assert audit.audit_lock(".").error
    # 脆弱性ありの終了コード 1 は失敗ではない
    monkeypatch.setattr(audit.subprocess, "run", fake((FIX / "uv_audit_cli.json").read_text(), 1))
    r = audit.audit_lock(".")
    assert r.error is None and len(r.ids) == 3


def test_nonzero_exit_without_ids_is_an_error(monkeypatch):
    out = (FIX / "uv_audit_empty.json").read_text()
    monkeypatch.setattr(audit.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 2, stdout=out, stderr="resolution failed"))
    r = audit.audit_lock(".")
    assert r.ids == [] and r.error and "2" in r.error


def test_parse_dedupes_ids_and_maps_packages():
    text = json.dumps({"vulnerabilities": [
        {"id": "GHSA-1", "dependency": {"name": "a", "version": "1.0"}},
        {"id": "GHSA-1", "dependency": {"name": "a", "version": "1.0"}},
        {"id": "GHSA-2", "dependency": {"name": "b", "version": "2.0"}},
        {"id": "GHSA-1", "dependency": {"name": "b", "version": "2.0"}},
    ]})
    r = audit.parse_audit_json(text)
    assert r.ids == ["GHSA-1", "GHSA-2"]
    assert r.packages == {"a==1.0": ["GHSA-1"], "b==2.0": ["GHSA-2", "GHSA-1"]}
