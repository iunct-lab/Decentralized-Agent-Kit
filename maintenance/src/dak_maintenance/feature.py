"""feature-sync pipeline: summarize NEW features (not fixes) of updated deps.

Deterministic changelog retrieval (changelog.get_changelog) + provider-neutral LLM
summarization. No open web search needed — the deps are already known.
"""

from __future__ import annotations

import re
import sys
from typing import Callable

from .changelog import get_changelog
from .jsonutil import extract_json
from .proposals import Proposal, drop_overlaps

# Dependabot writes one "Updates `<pkg>` from <a> to <b>" line per package in
# both group and single-package PR bodies; an ungrouped PR instead says
# "Bumps [<pkg>](url) from <a> to <b>.". Non-greedy so extras such as
# google-adk[a2a,db,mcp] do not end the match at their inner "]".
_PER_ITEM_RE = re.compile(r"Updates `([^`]+)` from (\S+) to (\S+)")
_SINGLE_RE = re.compile(r"Bumps? \[(.+?)\]\([^)]*\) from (\S+) to (\S+)\.")


def deps_from_prs(prs: list[dict]) -> tuple[list[dict], int]:
    """Merged `deps` PRs (`gh pr list --json body`) → [{"package","from","to"}],
    first-seen update per package, and the number of PRs with no from/to
    (e.g. range-only "requirement updates"), which is normal, not an error."""
    deps: list[dict] = []
    seen: set[str] = set()
    skipped = 0
    for pr in prs:
        body = pr.get("body") or ""
        matches = _PER_ITEM_RE.findall(body) or _SINGLE_RE.findall(body)
        if not matches:
            skipped += 1
            continue
        for name, frm, to in matches:
            if name not in seen:
                seen.add(name)
                deps.append({"package": name, "from": frm, "to": to})
    return deps, skipped


CompleteFn = Callable[[str], str]
ChangelogFn = Callable[[str, str, str], str]

_PROMPT = """You review a dependency's changelog for a project with this charter:
---
{charter}
---
Dependency: {package} ({from_version} -> {to_version})
Changelog:
---
{changelog}
---
Identify only genuinely NEW features (not bug fixes) worth adopting in the project.
For each, propose an adoption. Return an empty list if nothing is worth adopting.

Respond with ONLY a JSON array of objects:
[{{"title": "Adopt <feature> in {package} v{to_version}",
   "feature": "<what's new>", "component": "<agent|mcp-server|bff|cli>",
   "sketch": "<how to integrate>"}}]
"""


def propose_feature_adoptions(
    deps: list[dict],
    complete: CompleteFn,
    charter: str = "",
    get_changelog_fn: ChangelogFn = get_changelog,
    existing: list[dict] | None = None,
    max_items: int = 2,
) -> list[Proposal]:
    """deps: list of {"package","from","to"} for recently updated dependencies."""
    proposals: list[Proposal] = []
    attempted = failed = 0
    for dep in deps:
        pkg = dep.get("package", "")
        frm = dep.get("from", "")
        to = dep.get("to", "")
        changelog = dep.get("changelog") or get_changelog_fn(pkg, frm, to)
        if not (changelog or "").strip():
            continue
        prompt = _PROMPT.format(
            charter=charter[:4000], package=pkg, from_version=frm,
            to_version=to, changelog=changelog[:8000],
        )
        attempted += 1
        try:
            raw = complete(prompt)
        except Exception as e:  # e.g. a transient 503: skip this dep, keep the weekly run going
            failed += 1
            print(f"warn: {pkg} の提案を作れず飛ばした（{e}）", file=sys.stderr)
            continue
        data = extract_json(raw)
        for it in (data if isinstance(data, list) else []):
            if not isinstance(it, dict):
                continue
            title = str(it.get("title") or "").strip()
            if not title:
                continue
            body = (
                f"**新機能**: {it.get('feature', '')}\n"
                f"**対象コンポーネント**: {it.get('component', '')}\n\n"
                f"**導入スケッチ**: {it.get('sketch', '')}\n\n"
                f"依存: `{pkg}` {frm} -> {to}\n\n"
                f"_自動生成 (feature-sync)。人間が取り込み価値を最終判断する。_"
            )
            proposals.append(Proposal(title=title, body=body, labels=["feature-sync", "automation"]))
    if attempted and failed == attempted:
        # A wrong key, model or URL fails every call; do not report it as a
        # quiet week with no proposals.
        raise RuntimeError(f"every LLM call failed ({failed} deps); check MAINT_LLM_*")
    return drop_overlaps(proposals, existing or [], complete, max_items)
