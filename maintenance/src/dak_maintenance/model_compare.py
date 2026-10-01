"""Compare models on the maintenance prompts with fixed inputs (no web search, no changelog fetch).

Each case calls the same pipeline the scheduled workflows use (watch, feature-sync,
charter-review, the triage risk assessment) with a recorded copy of its inputs, so
models differ only in what they answer. Prices are passed in, not kept here: they
go stale quickly (docs/maintenance/model-choice.md).
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .jsonutil import extract_json
from .search import SearchResult
from .watch import propose_technologies
from .feature import propose_feature_adoptions
from .charter import review_charter
from .risk import LLMAssessor
from .llm_client import make_complete

CASES = ["watch", "feature-sync", "charter-review", "triage"]
FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "model_compare"
# What changelog.txt is the changelog of.
TRIAGE_DEP = ("httpx", "0.27.2", "0.28.0")
_EMPTY_OBJECT = re.compile(r"\{\s*\}")


@dataclass
class ModelSpec:
    name: str
    base_url: str
    model: str
    api_key_env: str  # the variable holding the key; the key itself is never in the file


@dataclass
class CaseResult:
    model: str
    case: str
    parsed_ok: bool
    count: int
    latency_sec: float
    prompt_tokens: int | None
    completion_tokens: int | None
    error: str
    titles: list[str] = field(default_factory=list)


def load_models(path: str) -> list[ModelSpec]:
    return [ModelSpec(m["name"], m.get("base_url", ""), m["model"], m.get("api_key_env", ""))
            for m in json.loads(Path(path).read_text(encoding="utf-8"))]


def load_inputs(directory: Path = FIXTURES) -> dict:
    return {
        "charter": (directory / "charter.md").read_text(encoding="utf-8"),
        "search_results": [SearchResult(**r) for r in json.loads((directory / "search_results.json").read_text(encoding="utf-8"))],
        "deps": json.loads((directory / "deps.json").read_text(encoding="utf-8")),
        "changelog": (directory / "changelog.txt").read_text(encoding="utf-8"),
    }


def run_case(case: str, complete: Callable[[str], str], inputs: dict) -> CaseResult:
    """Run one case. parsed_ok: every LLM answer was JSON and no call failed."""
    raws: list[str] = []
    errors: list[str] = []

    def recording(prompt: str) -> str:
        try:
            raw = complete(prompt)
        except Exception as e:  # e.g. a 400 for an unsupported temperature: record it, go on
            errors.append(str(e))
            raise
        raws.append(raw)
        return raw

    def search(query: str, k: int) -> list[SearchResult]:
        # Every query gets the whole copy (deduplicated by URL), so every result reaches the model.
        return inputs["search_results"]

    start = time.monotonic()
    titles: list[str] = []
    try:
        if case == "watch":
            titles = [p.title for p in propose_technologies(inputs["charter"], recording, search=search)]
        elif case == "feature-sync":
            titles = [p.title for p in propose_feature_adoptions(
                inputs["deps"], recording, charter=inputs["charter"], get_changelog_fn=lambda *a: "")]
        elif case == "charter-review":
            titles = [p.title for p in review_charter(inputs["charter"], recording, search=search)]
        elif case == "triage":
            verdict = LLMAssessor(recording).assess(*TRIAGE_DEP, inputs["changelog"])
            # The assessor falls back to the heuristic on a bad answer; only an LLM verdict counts.
            titles = [f"{verdict.level.value}: {verdict.summary}"] if verdict.tier == "llm" else []
        else:
            raise ValueError(f"unknown case: {case}")
    except Exception as e:
        if not errors:
            errors.append(str(e))
    latency = time.monotonic() - start
    parsed_ok = not errors and bool(raws) and all(_reads_as_json(r) for r in raws)
    return CaseResult("", case, parsed_ok, len(titles), latency, None, None, "; ".join(errors), titles)


def _reads_as_json(raw: str) -> bool:
    # extract_json gives {} for "no JSON" too; a literal {} is still JSON.
    return extract_json(raw) != {} or bool(_EMPTY_OBJECT.search(raw or ""))


def compare(models: list[ModelSpec], cases: list[str], inputs: dict, timeout: float = 120.0) -> list[CaseResult]:
    results = []
    for spec in models:
        usage = {"prompt_tokens": None, "completion_tokens": None}

        def on_usage(u, usage=usage):
            for k in usage:
                if u.get(k) is not None:
                    usage[k] = (usage[k] or 0) + u[k]

        # Only the spec's own settings: "" (not None) keeps make_complete off MAINT_LLM_*.
        complete = make_complete(timeout, base_url=spec.base_url, model=spec.model,
                                 api_key=os.getenv(spec.api_key_env, "") if spec.api_key_env else "",
                                 on_usage=on_usage)
        for case in cases:
            usage.update(prompt_tokens=None, completion_tokens=None)
            if complete is None:
                r = CaseResult("", case, False, 0, 0.0, None, None, "base_url と model が要る")
            else:
                r = run_case(case, complete, inputs)
                r.prompt_tokens, r.completion_tokens = usage["prompt_tokens"], usage["completion_tokens"]
            r.model = spec.name
            results.append(r)
    return results


def _cell(v) -> str:
    return "—" if v is None else str(v)


def render_table(results: list[CaseResult], prices: dict) -> str:
    """Markdown: one row per model x case, then the proposals each one made (to judge by eye)."""
    lines = [
        "| model | case | JSON | count | sec | prompt tokens | completion tokens | cost (USD) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        price = prices.get(r.model)
        if price is None:
            cost = "価格未指定"
        elif r.prompt_tokens is None and r.completion_tokens is None:
            cost = "—"
        else:
            cost = f"${((r.prompt_tokens or 0) * price['input'] + (r.completion_tokens or 0) * price['output']) / 1e6:.4f}"
        lines.append(f"| {r.model} | {r.case} | {'yes' if r.parsed_ok else 'no'} | {r.count} | {r.latency_sec:.1f} | "
                     f"{_cell(r.prompt_tokens)} | {_cell(r.completion_tokens)} | {cost} |")
    lines += ["", "## 出力"]
    for r in results:
        lines.append(f"- {r.model} / {r.case}: " + ("; ".join(r.titles) or "(なし)") + (f" — error: {r.error}" if r.error else ""))
    return "\n".join(lines) + "\n"
