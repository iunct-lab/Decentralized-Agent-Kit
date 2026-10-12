from dak_maintenance.search import SearchResult
from dak_maintenance.watch import propose_technologies, generate_queries, gather_candidates
from dak_maintenance.jsonutil import extract_json


def test_extract_json_array():
    assert extract_json('```json\n["a", "b"]\n```') == ["a", "b"]
    assert extract_json('prefix [1, 2] suffix') == [1, 2]


def test_generate_queries():
    complete = lambda p: '["mcp new spec", "a2a update"]'
    assert generate_queries("charter", complete) == ["mcp new spec", "a2a update"]


def test_gather_candidates_dedupes_by_url():
    def search(q, k):
        return [SearchResult("T", "http://x", "s"), SearchResult("T2", "http://x", "s")]
    got = gather_candidates(["q1", "q2"], search, k=5)
    assert len(got) == 1


def _fake_complete_factory():
    calls = {"n": 0}

    def complete(prompt: str) -> str:
        calls["n"] += 1
        if calls["n"] == 1:  # generate_queries
            return '["query one"]'
        # evaluate -> two proposals
        return (
            '[{"title": "[tech-watch] Foo の採用検討", "subject": "Foo", "url": "http://foo",'
            ' "fit": "MCP", "sketch": "add to mcp-server"},'
            ' {"title": "[tech-watch] Bar の採用検討", "subject": "Bar", "url": "http://bar",'
            ' "fit": "A2A", "sketch": "add to agent"}]'
        )
    return complete


def test_propose_technologies_end_to_end():
    def search(q, k):
        return [SearchResult("Foo lib", "http://foo", "great")]
    proposals = propose_technologies("charter text", _fake_complete_factory(), search=search, max_items=5)
    titles = [p.title for p in proposals]
    assert "[tech-watch] Foo の採用検討" in titles
    assert all("tech-watch" in p.labels for p in proposals)
    assert "http://foo" in proposals[0].body


def test_propose_technologies_drops_same_title_in_one_run_and_caps():
    foo = ('{"title": "[tech-watch] Foo の採用検討", "subject": "Foo", "url": "http://foo",'
           ' "fit": "MCP", "sketch": "add to mcp-server"}')
    answers = iter(['["query one"]', f"[{foo}, {foo.replace('Foo の', 'foo  の')}, {NEW_ONE}]"])
    proposals = propose_technologies(
        "charter", lambda p: next(answers), search=_one_search, existing=[], max_items=5,
    )
    assert [p.title for p in proposals] == ["[tech-watch] Foo の採用検討", "[tech-watch] Baz の採用検討"]


# #612 の提案と、それと内容が同じ #112・#322（開いた）、#611 の提案と閉じた #309 の写し。
EXISTING = [
    {"number": 112, "state": "OPEN",
     "title": "要望: MCP 2026-07-28 仕様（ステートレスコア / MRTR / Tasks 拡張 / Apps）への追随"},
    {"number": 322, "state": "OPEN",
     "title": "PBI: 2026-07-28 版の MCP クライアントからも DAK の MCP サーバをセッションなしで使え、"
              "DAK のエージェントからの接続も壊れない"},
    {"number": 309, "state": "CLOSED",
     "title": "PBI: DAK の A2A 窓口が現行の A2A 仕様 1.0 に沿っていることを、公式の適合テストと"
              "公式 SDK のクライアントで確かめられる"},
]
MCP_612 = (
    '{"title": "[tech-watch] MCP 2026-07-28 Specification への upgrade",'
    ' "subject": "Model Context Protocol (MCP) stateless core & OAuth 2.1",'
    ' "url": "https://blog.modelcontextprotocol.io/posts/2026-07-28",'
    ' "fit": "スコープ内ドメイン: MCP / エージェント基盤",'
    ' "sketch": "DAK の mcp-server / agent で MCP 2026-07-28 への update。stateless request model へのマイグレーション"}'
)
A2A_611 = (
    '{"title": "[tech-watch] A2A Protocol (Google/Linux Foundation) の採用検討",'
    ' "subject": "Agent-to-Agent (A2A) Protocol",'
    ' "url": "https://www.artificialintelligence-news.com/news/google-launches-a2a",'
    ' "fit": "スコープ内ドメイン: A2A（Agent-to-Agent）/ エージェント基盤",'
    ' "sketch": "DAK の agent / a2a-sdk コンポーネントで A2A 仕様 (2025-06) への upgrade を検討"}'
)
NEW_ONE = (
    '{"title": "[tech-watch] Baz の採用検討", "subject": "Baz", "url": "http://baz",'
    ' "fit": "MCP", "sketch": "add to mcp-server"}'
)


def _overlap_complete(evaluated: str, verdict: str, prompts: list[str]):
    """偽の LLM: 1 回目はクエリ、2 回目は提案、3 回目は既存との突き合わせに答える。"""
    answers = iter(['["query one"]', evaluated, verdict])

    def complete(prompt: str) -> str:
        prompts.append(prompt)
        return next(answers)
    return complete


def _one_search(q, k):
    return [SearchResult("x", "http://x", "s")]


def test_a_proposal_with_another_title_but_the_same_content_as_an_open_issue_is_dropped(capsys):
    prompts: list[str] = []
    complete = _overlap_complete(f"[{MCP_612}]", '[{"index": 1, "overlaps": 112}]', prompts)

    proposals = propose_technologies("charter", complete, search=_one_search, existing=EXISTING, max_items=2)

    assert proposals == []
    assert "#112" in prompts[-1] and "#322" in prompts[-1]
    assert "stateless request model" in prompts[-1]  # 題だけでなく本文も比べる
    err = capsys.readouterr().err
    assert "skip: [tech-watch] MCP 2026-07-28 Specification への upgrade" in err
    assert "#112" in err


def test_a_proposal_with_the_same_content_as_a_closed_issue_is_dropped(capsys):
    prompts: list[str] = []
    complete = _overlap_complete(f"[{A2A_611}]", '[{"index": 1, "overlaps": 309}]', prompts)

    proposals = propose_technologies("charter", complete, search=_one_search, existing=EXISTING, max_items=2)

    assert proposals == []
    assert "#309 [CLOSED]" in prompts[-1]
    assert "#309" in capsys.readouterr().err


def test_a_proposal_that_overlaps_nothing_is_kept_and_the_count_is_capped():
    prompts: list[str] = []
    complete = _overlap_complete(
        f"[{MCP_612}, {NEW_ONE}, {NEW_ONE.replace('Baz', 'Qux')}]",
        '[{"index": 1, "overlaps": 112}, {"index": 2, "overlaps": null}, {"index": 3, "overlaps": null}]',
        prompts,
    )

    proposals = propose_technologies("charter", complete, search=_one_search, existing=EXISTING, max_items=1)

    assert [p.title for p in proposals] == ["[tech-watch] Baz の採用検討"]


def test_an_overlap_after_the_cap_is_still_logged(capsys):
    complete = _overlap_complete(
        f"[{NEW_ONE}, {MCP_612}]", '[{"index": 1, "overlaps": null}, {"index": 2, "overlaps": 112}]', [],
    )

    proposals = propose_technologies("charter", complete, search=_one_search, existing=EXISTING, max_items=1)

    assert [p.title for p in proposals] == ["[tech-watch] Baz の採用検討"]
    assert "#112" in capsys.readouterr().err


def test_an_index_written_as_a_string_still_drops_the_proposal(capsys):
    complete = _overlap_complete(f"[{MCP_612}]", '[{"index": "1", "overlaps": 112}]', [])

    proposals = propose_technologies("charter", complete, search=_one_search, existing=EXISTING, max_items=2)

    assert proposals == []
    assert "#112" in capsys.readouterr().err


def test_an_overlap_written_as_a_string_with_a_hash_still_drops_the_proposal(capsys):
    complete = _overlap_complete(f"[{MCP_612}]", '[{"index": 1, "overlaps": "#112"}]', [])

    proposals = propose_technologies("charter", complete, search=_one_search, existing=EXISTING, max_items=2)

    assert proposals == []
    assert "#112" in capsys.readouterr().err


def test_an_overlap_with_a_number_that_is_not_an_existing_issue_does_not_drop_the_proposal():
    complete = _overlap_complete(f"[{NEW_ONE}]", '[{"index": 1, "overlaps": 9999}]', [])

    proposals = propose_technologies("charter", complete, search=_one_search, existing=EXISTING, max_items=2)

    assert [p.title for p in proposals] == ["[tech-watch] Baz の採用検討"]


def test_an_unreadable_overlap_answer_fails_the_run():
    import pytest

    complete = _overlap_complete(f"[{NEW_ONE}]", "sorry, I cannot help", [])

    with pytest.raises(RuntimeError, match="突き合わせられなかった"):
        propose_technologies("charter", complete, search=_one_search, existing=EXISTING, max_items=2)


def test_propose_technologies_caps_to_max():
    def search(q, k):
        return [SearchResult("Foo", "http://foo", "s")]
    proposals = propose_technologies("charter", _fake_complete_factory(), search=search, max_items=1)
    assert len(proposals) == 1


def test_no_candidates_returns_empty():
    proposals = propose_technologies("charter", lambda p: "[]", search=lambda q, k: [], max_items=5)
    assert proposals == []
