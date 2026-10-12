"""Shared proposal model for tech-watch / feature-sync / charter-review."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from typing import Callable

from .jsonutil import extract_json

CompleteFn = Callable[[str], str]


@dataclass
class Proposal:
    title: str
    body: str
    labels: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"title": self.title, "body": self.body, "labels": self.labels}


def _norm(title: str) -> str:
    return re.sub(r"\s+", " ", (title or "").strip().lower())


def dedupe(proposals: list[Proposal], existing_titles: list[str], max_items: int) -> list[Proposal]:
    """Drop proposals whose title matches an existing issue, then cap the count."""
    seen = {_norm(t) for t in (existing_titles or [])}
    out: list[Proposal] = []
    for p in proposals:
        key = _norm(p.title)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(p)
        if len(out) >= max_items:
            break
    return out


_OVERLAP_PROMPT = """You compare new proposals with a project's existing issues (requests and PBIs,
open and closed). A proposal overlaps an existing issue when it asks for the same
thing in substance, even if the wording or title differs. A closed issue means the
work is done or was decided against; a proposal for the same thing still overlaps.

Proposals:
{proposals}

Existing issues:
{existing}

Respond with ONLY a JSON array, one object per proposal:
[{{"index": <proposal number>, "overlaps": <existing issue number, or null>}}]
"""


def _number(value) -> int | None:
    """An LLM may write 112, "112" or "#112" (the prompt lists issues as #N)."""
    if type(value) is int:
        return value
    if isinstance(value, str) and value.strip().lstrip("#").isdigit():
        return int(value.strip().lstrip("#"))
    return None


def drop_overlaps(
    proposals: list[Proposal], existing: list[dict], complete: CompleteFn, max_items: int,
) -> list[Proposal]:
    """Drop proposals whose content overlaps an existing issue (one LLM call), then
    same-title duplicates within the run, then cap the count."""
    overlaps: dict[int, int] = {}
    if proposals and existing:
        numbers = {i.get("number") for i in existing}
        listed = "\n".join(
            f"{n}. {p.title}\n{p.body[:500]}" for n, p in enumerate(proposals, 1)
        )
        known = "\n".join(f"#{i.get('number')} [{i.get('state')}] {i.get('title')}" for i in existing)
        data = extract_json(complete(_OVERLAP_PROMPT.format(proposals=listed, existing=known)))
        if not isinstance(data, list):
            # Filing duplicates silently would hide the failure; stop the run instead.
            raise RuntimeError("提案と既存の Issue を突き合わせられなかった（LLM の応答が JSON の配列ではない）")
        for it in data:
            if isinstance(it, dict) and _number(it.get("overlaps")) in numbers:
                overlaps[_number(it.get("index"))] = _number(it["overlaps"])
    seen: set[str] = set()
    out: list[Proposal] = []
    for n, p in enumerate(proposals, 1):
        if n in overlaps:
            print(f"skip: {p.title}（#{overlaps[n]} と重なる）", file=sys.stderr)
            continue
        key = _norm(p.title)
        if not key or key in seen or len(out) >= max_items:
            continue  # keep going past the cap so every overlap above is logged
        seen.add(key)
        out.append(p)
    return out
