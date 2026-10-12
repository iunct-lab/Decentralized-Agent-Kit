from dak_maintenance.feature import propose_feature_adoptions
from dak_maintenance.charter import review_charter
from dak_maintenance.search import SearchResult


def test_feature_sync_summarizes_new_features():
    def complete(prompt: str) -> str:
        return ('[{"title": "Adopt streaming tools in litellm v1.52.0",'
                ' "feature": "native streaming tool calls", "component": "agent",'
                ' "sketch": "use in meta_llm"}]')
    deps = [{"package": "litellm", "from": "1.51.0", "to": "1.52.0", "changelog": "Added streaming."}]
    proposals = propose_feature_adoptions(deps, complete, max_items=5)
    assert proposals[0].title == "Adopt streaming tools in litellm v1.52.0"
    assert "feature-sync" in proposals[0].labels
    assert "litellm" in proposals[0].body


def test_feature_sync_skips_deps_without_changelog():
    called = {"n": 0}

    def complete(prompt: str) -> str:
        called["n"] += 1
        return "[]"

    # changelog 取得関数が空を返す → LLM は呼ばれない
    deps = [{"package": "x", "from": "1.0.0", "to": "1.0.1"}]
    proposals = propose_feature_adoptions(deps, complete, get_changelog_fn=lambda p, a, b: "", max_items=5)
    assert proposals == []
    assert called["n"] == 0


def test_charter_review_produces_single_issue():
    def complete(prompt: str) -> str:
        return ('{"title": "Charter review 2026-Q3", "landscape": "MCP evolving",'
                ' "revisions": "- add domain X", "domains": "add X"}')
    proposals = review_charter("charter", complete, search=lambda q, k: [], quarter="2026-Q3")
    assert len(proposals) == 1
    assert proposals[0].title == "Charter review 2026-Q3"
    assert "charter" in proposals[0].labels


def test_charter_review_dedupes_existing():
    def complete(prompt: str) -> str:
        return '{"title": "Charter review 2026-Q3", "landscape": "x", "revisions": "y", "domains": "z"}'
    proposals = review_charter(
        "charter", complete, search=lambda q, k: [SearchResult("t", "u", "s")],
        quarter="2026-Q3", existing_titles=["Charter review 2026-Q3"],
    )
    assert proposals == []


def test_a_failing_llm_call_skips_only_that_dependency(capsys):
    """One transient LLM error (e.g. 503) must not fail the whole weekly run."""
    from dak_maintenance.feature import propose_feature_adoptions

    def complete(prompt):
        if "pkg-a" in prompt:
            raise RuntimeError("Server error '503 Service Unavailable'")
        return '[{"title": "Adopt B feature", "feature": "f", "component": "agent", "sketch": "s"}]'

    deps = [{"package": "pkg-a", "from": "1", "to": "2", "changelog": "New: x"},
            {"package": "pkg-b", "from": "1", "to": "2", "changelog": "New: y"}]

    proposals = propose_feature_adoptions(deps, complete)

    assert [p.title for p in proposals] == ["Adopt B feature"]
    assert "pkg-a" in capsys.readouterr().err


def test_every_llm_call_failing_fails_the_run():
    """A wrong key/model/URL makes every call fail; that must not look like a
    quiet week with no proposals."""
    import pytest

    from dak_maintenance.feature import propose_feature_adoptions

    def complete(prompt):
        raise RuntimeError("401 Unauthorized")

    deps = [{"package": "a", "from": "1", "to": "2", "changelog": "New: x"},
            {"package": "b", "from": "1", "to": "2", "changelog": "New: y"}]

    with pytest.raises(RuntimeError, match="every LLM call failed"):
        propose_feature_adoptions(deps, complete)


def test_a_prompt_template_error_is_not_swallowed(monkeypatch):
    import pytest

    from dak_maintenance import feature

    monkeypatch.setattr(feature, "_PROMPT", "{undefined_placeholder}")
    with pytest.raises(KeyError):
        feature.propose_feature_adoptions(
            [{"package": "a", "from": "1", "to": "2", "changelog": "New: x"}], lambda p: "[]")


def test_feature_sync_drops_proposals_that_overlap_open_or_closed_issues(capsys):
    """tech-watch と同じく、題が違っても既存の要望・PBI（開いた・閉じた）と内容が同じ提案は残さない。"""
    prompts: list[str] = []

    def complete(prompt: str) -> str:
        prompts.append(prompt)
        if "Existing issues" in prompt:
            return ('[{"index": 1, "overlaps": 322}, {"index": 2, "overlaps": 309},'
                    ' {"index": 3, "overlaps": null}]')
        return ('[{"title": "Adopt stateless transport in mcp v2.0.0", "feature": "sessionless HTTP",'
                ' "component": "mcp-server", "sketch": "serve without sessions"},'
                ' {"title": "Adopt A2A 1.0 in a2a-sdk v1.0.0", "feature": "A2A 1.0",'
                ' "component": "agent", "sketch": "move the A2A endpoint to 1.0"},'
                ' {"title": "Adopt structured output in litellm v1.52.0", "feature": "json schema",'
                ' "component": "agent", "sketch": "use in meta_llm"}]')

    existing = [
        {"number": 322, "state": "OPEN",
         "title": "PBI: 2026-07-28 版の MCP クライアントからも DAK の MCP サーバをセッションなしで使え、"
                  "DAK のエージェントからの接続も壊れない"},
        {"number": 309, "state": "CLOSED",
         "title": "PBI: DAK の A2A 窓口が現行の A2A 仕様 1.0 に沿っていることを、公式の適合テストと"
                  "公式 SDK のクライアントで確かめられる"},
    ]
    deps = [{"package": "mcp", "from": "1.9.0", "to": "2.0.0", "changelog": "New: stateless."}]

    proposals = propose_feature_adoptions(deps, complete, existing=existing, max_items=2)

    assert [p.title for p in proposals] == ["Adopt structured output in litellm v1.52.0"]
    assert "#322 [OPEN]" in prompts[-1] and "#309 [CLOSED]" in prompts[-1]
    err = capsys.readouterr().err
    assert "#322" in err and "#309" in err
