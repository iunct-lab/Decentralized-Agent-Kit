"""tech-watch pipeline: charter-driven web search + provider-neutral LLM evaluation.

Pure orchestration over injected `complete` (LLM) and `search` (web) callables,
so it is fully unit-testable with fakes (no network, no model, no vendor lock).

Flow:
  1. LLM turns the charter's in-scope domains into a few search queries.
  2. web search (Tavily) gathers candidate technologies.
  3. LLM evaluates candidates against the charter's adoption criteria -> proposals.
"""

from __future__ import annotations

from typing import Callable

from .jsonutil import extract_json
from .proposals import Proposal, drop_overlaps
from .search import SearchResult, web_search

CompleteFn = Callable[[str], str]
SearchFn = Callable[[str, int], list[SearchResult]]

_QUERY_PROMPT = """You are a technology scout for a project with this charter:
---
{charter}
---
Produce 3-5 web search queries that would surface NEW libraries, specs, or
techniques relevant to the charter's in-scope domains.
Respond with ONLY a JSON array of query strings.
"""

_EVAL_PROMPT = """You evaluate candidate technologies against a project charter.
Charter:
---
{charter}
---
Candidates (from web search):
{candidates}

For each candidate that satisfies ALL of the charter's adoption criteria and is
clearly relevant, produce a proposal. Reject anything off-scope, vendor-locked, or
principle-violating. It is fine to return an empty list.

Respond with ONLY a JSON array of objects:
[{{"title": "[tech-watch] <subject> の採用検討",
   "subject": "<name>", "url": "<reference>",
   "fit": "<which domains/criteria it satisfies>",
   "sketch": "<how to integrate, which component>"}}]
"""


def generate_queries(charter: str, complete: CompleteFn) -> list[str]:
    data = extract_json(complete(_QUERY_PROMPT.format(charter=charter[:6000])))
    if isinstance(data, list):
        return [str(q) for q in data if str(q).strip()][:5]
    return []


def gather_candidates(queries: list[str], search: SearchFn, k: int = 5) -> list[SearchResult]:
    seen: set[str] = set()
    out: list[SearchResult] = []
    for q in queries:
        for r in search(q, k):
            if r.url and r.url not in seen:
                seen.add(r.url)
                out.append(r)
    return out


def _format_candidates(candidates: list[SearchResult]) -> str:
    return "\n".join(f"- {c.title} ({c.url}): {c.snippet}" for c in candidates) or "(none)"


def evaluate(charter: str, candidates: list[SearchResult], complete: CompleteFn) -> list[Proposal]:
    if not candidates:
        return []
    raw = complete(_EVAL_PROMPT.format(charter=charter[:6000], candidates=_format_candidates(candidates)))
    data = extract_json(raw)
    items = data if isinstance(data, list) else []
    proposals: list[Proposal] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        title = str(it.get("title") or "").strip()
        if not title:
            continue
        body = (
            f"**対象**: {it.get('subject', '')}\n"
            f"**参照**: {it.get('url', '')}\n\n"
            f"**憲章適合度**: {it.get('fit', '')}\n\n"
            f"**導入スケッチ**: {it.get('sketch', '')}\n\n"
            f"_自動生成 (tech-watch)。docs/CHARTER.md の採用基準に照らして人間が最終判断する。_"
        )
        proposals.append(Proposal(title=title, body=body, labels=["tech-watch", "automation"]))
    return proposals


def propose_technologies(
    charter: str,
    complete: CompleteFn,
    search: SearchFn = web_search,
    existing: list[dict] | None = None,
    max_items: int = 2,
    k: int = 5,
) -> list[Proposal]:
    queries = generate_queries(charter, complete)
    candidates = gather_candidates(queries, search, k)
    proposals = evaluate(charter, candidates, complete)
    return drop_overlaps(proposals, existing or [], complete, max_items)
