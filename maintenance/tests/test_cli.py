import json

import pytest

from dak_maintenance.cli import main


def test_triage_single_dep_still_works(tmp_path, capsys):
    changelog = tmp_path / "changelog.txt"
    changelog.write_text("Fixed a typo and improved docs.")
    rc = main([
        "triage", "--package", "pkg", "--from", "1.0.0", "--to", "1.0.1",
        "--ci-passed", "true", "--changelog-file", str(changelog),
    ])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["packages"] == ["pkg"]
    assert out["decision"]["action"] == "auto-merge"


def test_triage_deps_json_combines_worst_case(capsys):
    deps = [
        {"package": "a", "from": "1.0.0", "to": "1.0.1"},
        {"package": "b", "from": "1.0.0", "to": "2.0.0"},
    ]
    rc = main([
        "triage", "--deps-json", json.dumps(deps), "--ci-passed", "true",
    ])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["packages"] == ["a", "b"]
    assert out["bump"] == "major"
    assert out["decision"]["action"] == "needs-human-review"
    assert len(out["per_dependency"]) == 2
    assert "内訳" in out["decision"]["reason"]


def test_triage_missing_package_and_deps_json_errors():
    try:
        main(["triage", "--ci-passed", "true"])
        assert False, "expected SystemExit"
    except SystemExit as e:
        assert e.code == 2


@pytest.mark.parametrize("bad_json", [
    "not json",                      # JSON として不正
    '{"package": "a"}',              # 配列でなくオブジェクト
    '[{"from": "1.0", "to": "2.0"}]',  # package キー欠落
    '["just-a-string"]',             # 要素が dict でない
])
def test_triage_malformed_deps_json_exits_gracefully(bad_json, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["triage", "--deps-json", bad_json, "--ci-passed", "true"])
    assert exc.value.code == 2
    assert "deps-json" in capsys.readouterr().err


@pytest.mark.parametrize("command", [["watch"], ["feature-sync", "--deps", "[]"]])
def test_existing_issues_are_read_from_the_file(command, tmp_path, monkeypatch, capsys):
    from dak_maintenance import cli

    issues = [{"number": 112, "title": "要望: MCP", "state": "OPEN"}]
    path = tmp_path / "existing.json"
    path.write_text(json.dumps(issues, ensure_ascii=False))
    seen = {}

    def fake(*args, existing=None, **kwargs):
        seen["existing"] = existing
        return []

    monkeypatch.setattr(cli, "_require_complete", lambda: (lambda p: "[]"))
    monkeypatch.setattr(cli, "propose_technologies", fake)
    monkeypatch.setattr(cli, "propose_feature_adoptions", fake)
    (tmp_path / "CHARTER.md").write_text("charter")

    rc = main([*command, "--charter", str(tmp_path / "CHARTER.md"), "--existing-issues", str(path)])

    assert rc == 0
    assert seen["existing"] == issues


def test_a_missing_existing_issues_file_is_no_existing_issues(tmp_path, monkeypatch):
    from dak_maintenance import cli

    seen = {}

    def fake(*args, existing=None, **kwargs):
        seen["existing"] = existing
        return []

    monkeypatch.setattr(cli, "_require_complete", lambda: (lambda p: "[]"))
    monkeypatch.setattr(cli, "propose_technologies", fake)
    (tmp_path / "CHARTER.md").write_text("charter")

    rc = main(["watch", "--charter", str(tmp_path / "CHARTER.md"),
               "--existing-issues", str(tmp_path / "nothing.json")])

    assert rc == 0
    assert seen["existing"] == []
