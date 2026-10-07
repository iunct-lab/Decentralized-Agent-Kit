"""Tests for request_issue.py (run: `uv run --no-project --with pytest pytest scripts/setup -q`).
`gh` is replaced by a fake that records every call."""
import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("request_issue", Path(__file__).with_name("request_issue.py"))
request_issue = importlib.util.module_from_spec(spec)
spec.loader.exec_module(request_issue)

PROJECT_URL = "https://github.com/users/octo/projects/7"
ISSUE_URL = "https://github.com/octo/repo/issues/42"


class FakeGh:
    def __init__(self, fail_on=None):
        self.calls = []  # (args, env GH_TOKEN)
        self.bodies = []
        self.fail_on = fail_on  # (subcommand, n-th call) that raises
        self.created = 0

    def __call__(self, args, env=None):
        self.calls.append((args, (env or {}).get("GH_TOKEN")))
        if self.fail_on and args[:2] == list(self.fail_on[:2]) and \
                sum(1 for c, _ in self.calls if c[:2] == args[:2]) == self.fail_on[2]:
            raise SystemExit(f"gh {' '.join(args[:2])} failed: HTTP 403 (missing project scope)")
        if args[:2] == ["issue", "create"]:
            self.created += 1
            self.bodies.append(Path(args[args.index("--body-file") + 1]).read_text(encoding="utf-8"))
            return f"{ISSUE_URL[:-2]}{41 + self.created}\n"
        if args[:2] == ["project", "item-add"]:
            return json.dumps({"id": "ITEM"})
        if args[:2] == ["project", "field-list"]:
            return json.dumps({"fields": [
                {"id": "F-other", "name": "Priority", "options": [{"id": "p0", "name": "P0"}]},
                {"id": "F-status", "name": "Status", "options": [
                    {"id": "o-todo", "name": "Ready"}, {"id": "o-backlog", "name": "Backlog"}]},
            ]})
        if args[:2] == ["project", "view"]:
            return json.dumps({"id": "PROJ"})
        return ""


def _proposals(tmp_path, items):
    path = tmp_path / "proposals.json"
    path.write_text(json.dumps(items))
    return path


PROPOSAL = {"title": "Adopt X", "body": "**新機能**: X を使う", "labels": ["feature-sync", "automation"]}


def test_proposal_becomes_a_request_issue_and_lands_in_the_backlog(tmp_path, monkeypatch):
    gh = FakeGh()
    monkeypatch.setattr(request_issue, "gh", gh)
    monkeypatch.setenv("DAK_PROJECT_URL", PROJECT_URL)
    monkeypatch.setenv("DAK_PROJECT_TOKEN", "project-token")

    assert request_issue.main([str(_proposals(tmp_path, [PROPOSAL])), "--source", "feature-sync"]) == 0

    create = gh.calls[0][0]
    assert create[:2] == ["issue", "create"]
    assert create[create.index("--title") + 1] == "要望: Adopt X"
    assert create[create.index("--label") + 1] == "feature-sync,automation,type:request"
    assert gh.bodies[0].startswith("## 要望\n\n**新機能**: X を使う\n")
    add, fields, view, edit = (c for c in gh.calls[1:])
    assert add[0] == ["project", "item-add", "7", "--owner", "octo", "--url", ISSUE_URL, "--format", "json"]
    assert fields[0][:2] == ["project", "field-list"] and view[0][:2] == ["project", "view"]
    assert edit[0] == ["project", "item-edit", "--id", "ITEM", "--project-id", "PROJ",
                       "--field-id", "F-status", "--single-select-option-id", "o-backlog"]
    assert {token for _, token in gh.calls[1:]} == {"project-token"}  # Project calls use the Project token
    assert gh.calls[0][1] is None  # the issue itself is created with the workflow's own token


def test_request_body_is_a_tidy_request_not_a_verbatim_quote():
    body = request_issue.request_body("**新機能**: X を使う", "tech-watch")

    assert body.startswith("## 要望\n\n**新機能**: X を使う\n")
    assert "- 経路: tech-watch" in body
    assert "## 派生 PBI" in body
    for old in ("## 原文", "一字も", "そのまま保存", "```"):
        assert old not in body


def test_feature_request_template_uses_request_sections():
    """The hand-written request has the same sections as request_body. A form writes each label as a `### <label>`
    heading; the backlog lint reads `##` and `###` alike."""
    template = (Path(__file__).resolve().parents[2] / ".github/ISSUE_TEMPLATE/feature_request.yml").read_text(encoding="utf-8")

    assert 'labels: ["type:request"]' in template
    assert 'title: "要望: "' in template
    for label in ("label: 要望\n", "label: 出典\n", "label: 派生 PBI\n"):
        assert label in template
    assert "原文" not in template
    derived = request_issue.request_body("x", "y").split("## 派生 PBI\n\n")[1].split("。")[0]
    assert f"value: {derived}。" in template  # same "not filed yet" marker as the scheduled requests


@pytest.mark.parametrize("title", ["要望: Adopt X", "要望:Adopt X", "要望:   Adopt X"])
def test_title_that_already_says_request_is_normalized_not_prefixed_twice(tmp_path, monkeypatch, title):
    """Same normalization as the workflows' dedupe (`sub("^要望: *"; "")`)."""
    gh = FakeGh()
    monkeypatch.setattr(request_issue, "gh", gh)
    monkeypatch.delenv("DAK_PROJECT_URL", raising=False)

    request_issue.main([str(_proposals(tmp_path, [dict(PROPOSAL, title=title)])), "--source", "x"])

    create = gh.calls[0][0]
    assert create[create.index("--title") + 1] == "要望: Adopt X"
    assert len(gh.calls) == 1  # no Project configured: only the issue


def test_project_configured_without_token_fails_after_creating_the_issue(tmp_path, monkeypatch, capsys):
    gh = FakeGh()
    monkeypatch.setattr(request_issue, "gh", gh)
    monkeypatch.setenv("DAK_PROJECT_URL", PROJECT_URL)
    monkeypatch.delenv("DAK_PROJECT_TOKEN", raising=False)

    assert request_issue.main([str(_proposals(tmp_path, [PROPOSAL])), "--source", "x"]) == 1
    assert [c[0][:2] for c in gh.calls] == [["issue", "create"]]
    assert "DAK_PROJECT_TOKEN" in capsys.readouterr().err


@pytest.mark.parametrize("text", ["", "[]"])
def test_no_proposals_is_zero_issues(tmp_path, monkeypatch, capsys, text):
    gh = FakeGh()
    monkeypatch.setattr(request_issue, "gh", gh)
    path = tmp_path / "proposals.json"
    path.write_text(text)

    assert request_issue.main([str(path), "--source", "x"]) == 0
    assert gh.calls == []
    assert "0 proposal(s)" in capsys.readouterr().out


def test_org_project_url_is_understood():
    assert request_issue.parse_project_url("https://github.com/orgs/acme/projects/12") == ("acme", "12")
    with pytest.raises(SystemExit):
        request_issue.parse_project_url("https://example.com/not-a-project")


def test_one_failure_does_not_stop_the_other_proposals(tmp_path, monkeypatch, capsys):
    gh = FakeGh(fail_on=("project", "item-add", 1))
    monkeypatch.setattr(request_issue, "gh", gh)
    monkeypatch.setenv("DAK_PROJECT_URL", PROJECT_URL)
    monkeypatch.setenv("DAK_PROJECT_TOKEN", "t")

    rc = request_issue.main([str(_proposals(tmp_path, [PROPOSAL, dict(PROPOSAL, title="Adopt Y")])), "--source", "x"])

    assert rc == 1  # the run is reported as failed...
    assert gh.created == 2  # ...but the second proposal was still filed and added
    assert sum(1 for c, _ in gh.calls if c[:2] == ["project", "item-edit"]) == 1
    assert "missing project scope" in capsys.readouterr().err


def test_gh_failure_shows_gh_stderr(monkeypatch):
    import subprocess

    def fail(*a, **k):
        raise subprocess.CalledProcessError(1, ["gh"], output="", stderr="could not add label: 'type:request' not found")

    monkeypatch.setattr(request_issue.subprocess, "run", fail)
    with pytest.raises(SystemExit, match="'type:request' not found"):
        request_issue.gh(["issue", "create"])


def test_missing_backlog_option_says_how_to_add_it(tmp_path, monkeypatch, capsys):
    gh = FakeGh()
    real = gh.__call__

    def no_backlog(args, env=None):
        out = real(args, env)
        if args[:2] == ["project", "field-list"]:
            return json.dumps({"fields": [{"id": "F", "name": "Status", "options": [{"id": "o", "name": "Todo"}]}]})
        return out

    monkeypatch.setattr(request_issue, "gh", no_backlog)
    monkeypatch.setenv("DAK_PROJECT_URL", PROJECT_URL)
    monkeypatch.setenv("DAK_PROJECT_TOKEN", "t")

    assert request_issue.main([str(_proposals(tmp_path, [PROPOSAL])), "--source", "x"]) == 1
    err = capsys.readouterr().err
    assert "Add option 'Backlog'" in err
