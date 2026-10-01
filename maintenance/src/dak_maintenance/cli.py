"""`dak-maint` CLI. Provider-neutral (Gemini/Ollama/OpenAI/... via MAINT_LLM_*).

Subcommands:
  triage         依存更新を判定（auto-merge / needs-review）
  watch          憲章に沿う新技術を探索し提案 JSON を出力（tech-watch WF が Issue 化）
  feature-sync   依存の新機能取り込み提案 JSON を出力
  collect-deps   マージ済み deps PR の本文（標準入力の JSON）から依存の一覧を出力
  charter-review 憲章の見直し提案 JSON を出力
  compare-models 固定の入力で複数のモデルに保守のプロンプトを投げ、結果を Markdown の表にする

reasoning 系（watch/feature-sync/charter-review）は LLM 必須。MAINT_LLM_* 未設定なら
proposals は空を返す（ワークフローは 0 件として扱う）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .semver import classify_update, combine_bump
from .risk import HeuristicAssessor, LLMAssessor, assess_risk, combine_risk
from .decide import decide
from .changelog import get_changelog
from .llm_client import make_complete
from .watch import propose_technologies
from .feature import deps_from_prs, propose_feature_adoptions
from .charter import review_charter
from . import model_compare


def _bool(s: str) -> bool:
    return str(s).strip().lower() in {"1", "true", "yes", "success", "green"}


def _non_negative_int(s: str) -> int:
    n = int(s)
    if n < 0:
        raise argparse.ArgumentTypeError("0 以上")
    return n


def _read(path: str) -> str:
    try:
        return open(path, encoding="utf-8").read()
    except OSError as e:
        print(f"warn: {path} 読み込み失敗: {e}", file=sys.stderr)
        return ""


def _require_complete():
    complete = make_complete()
    if complete is None:
        print("warn: MAINT_LLM_* 未設定のため LLM 判定をスキップ（proposals=[]）。", file=sys.stderr)
    return complete


def _emit_proposals(proposals) -> int:
    print(json.dumps([p.to_dict() for p in proposals], ensure_ascii=False, indent=2))
    return 0


# ---- triage (Tier0/1) ----

def _load_deps(args: argparse.Namespace) -> list[tuple[str, str, str]]:
    """Return (package, from_version, to_version) triples for this triage run.

    `--deps-json` covers grouped Dependabot PRs (multiple packages in one PR);
    `--package/--from/--to` remains for single-dependency callers (e.g. the
    agent's dependency_maintenance skill).
    """
    if args.deps_json:
        try:
            raw = json.loads(args.deps_json)
            return [(d["package"], d.get("from", ""), d.get("to", "")) for d in raw]
        except (ValueError, TypeError, KeyError, AttributeError) as e:
            print(f"error: --deps-json の形式が不正 ({e.__class__.__name__}: {e})。"
                  ' 期待形式: [{"package": ..., "from": ..., "to": ...}, ...]', file=sys.stderr)
            raise SystemExit(2)
    if not args.package:
        print("error: --package/--from/--to または --deps-json のいずれかが必要", file=sys.stderr)
        raise SystemExit(2)
    return [(args.package, args.from_version, args.to_version)]


def cmd_triage(args: argparse.Namespace) -> int:
    deps = _load_deps(args)
    if args.assessor == "llm":
        complete = make_complete()
        assessor = LLMAssessor(complete, tier="llm") if complete else HeuristicAssessor()
        if complete is None:
            print("warn: ASSESSOR=llm だが MAINT_LLM_* 未設定。heuristic にフォールバック。", file=sys.stderr)
    else:
        assessor = HeuristicAssessor()

    per_dep = []
    for package, from_version, to_version in deps:
        bump = classify_update(from_version, to_version)
        changelog = _read(args.changelog_file) if args.changelog_file else (
            get_changelog(package, from_version, to_version) if args.fetch_changelog else ""
        )
        risk = assess_risk(package, from_version, to_version, changelog, assessor)
        per_dep.append((package, bump, risk))

    bump = combine_bump([b for _, b, _ in per_dep])
    risk = combine_risk([r for _, _, r in per_dep])
    decision = decide(bump, _bool(args.ci_passed), risk)

    reason = decision.reason
    if len(per_dep) > 1:
        detail = "; ".join(f"{p}: bump={b.value},risk={r.level.value}" for p, b, r in per_dep)
        reason = f"{reason} [内訳: {detail}]"

    result = {
        "packages": [p for p, _, _ in per_dep],
        "bump": bump.value,
        "risk": {"level": risk.level.value, "summary": risk.summary, "tier": risk.tier},
        "decision": {"action": decision.action, "reason": reason, "labels": decision.labels},
        "per_dependency": [
            {"package": p, "bump": b.value, "risk": r.level.value, "summary": r.summary}
            for p, b, r in per_dep
        ],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    gh_out = os.getenv("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as f:
            f.write(f"action={decision.action}\nbump={bump.value}\nrisk={risk.level.value}\n")
            f.write("labels=" + ",".join(decision.labels) + "\n")
            f.write("reason=" + reason.replace("\n", " ") + "\n")
    return 0


# ---- watch (Tier2, provider-neutral) ----

def cmd_watch(args: argparse.Namespace) -> int:
    complete = _require_complete()
    if complete is None:
        return _emit_proposals([])
    existing = json.loads(args.existing_titles) if args.existing_titles else []
    proposals = propose_technologies(
        _read(args.charter), complete, existing_titles=existing, max_items=args.max_items,
    )
    return _emit_proposals(proposals)


def cmd_feature_sync(args: argparse.Namespace) -> int:
    complete = _require_complete()
    if complete is None:
        return _emit_proposals([])
    deps = json.loads(args.deps) if args.deps else []
    existing = json.loads(args.existing_titles) if args.existing_titles else []
    proposals = propose_feature_adoptions(
        deps, complete, charter=_read(args.charter) if args.charter else "",
        existing_titles=existing, max_items=args.max_items,
    )
    return _emit_proposals(proposals)


def cmd_collect_deps(args: argparse.Namespace) -> int:
    """stdin: `gh pr list --json body` の出力。stdout: [{"package","from","to"}]。"""
    text = sys.stdin.read()
    try:
        prs = json.loads(text)
    except ValueError as e:
        # Empty stdin usually means the upstream `gh pr list` failed.
        print(f"error: 標準入力が PR の JSON ではない（{'空' if not text.strip() else e}）。"
              "gh pr list … --json body の出力を渡す", file=sys.stderr)
        return 2
    deps, skipped = deps_from_prs(prs)
    if skipped:
        print(f"note: {skipped} 件の deps-labeled PR は from/to を抽出できず対象外", file=sys.stderr)
    json.dump(deps[: args.max_items], sys.stdout)
    return 0


def cmd_charter_review(args: argparse.Namespace) -> int:
    complete = _require_complete()
    if complete is None:
        return _emit_proposals([])
    existing = json.loads(args.existing_titles) if args.existing_titles else []
    proposals = review_charter(
        _read(args.charter), complete, quarter=args.quarter, existing_titles=existing,
    )
    return _emit_proposals(proposals)


def cmd_compare_models(args: argparse.Namespace) -> int:
    cases = [c.strip() for c in args.cases.split(",") if c.strip()]
    unknown = [c for c in cases if c not in model_compare.CASES]
    if unknown:
        print(f"error: 未知のケース {unknown}（使えるのは {','.join(model_compare.CASES)}）", file=sys.stderr)
        return 2
    # Everything is checked before any model is called: a typo must not throw away a paid comparison.
    try:
        prices = json.loads(open(args.prices, encoding="utf-8").read()) if args.prices else {}
        model_compare.check_prices(prices)
    except (OSError, ValueError) as e:
        print(f"error: --prices を読めない（{e}）。期待形式: {{\"<name>\": {{\"input\": 0.1, \"output\": 0.5}}}}", file=sys.stderr)
        return 2
    try:
        out = open(args.out, "w", encoding="utf-8") if args.out else sys.stdout
    except OSError as e:
        print(f"error: --out に書けない（{e}）", file=sys.stderr)
        return 2
    results = model_compare.compare(model_compare.load_models(args.models), cases, model_compare.load_inputs())
    out.write(model_compare.render_table(results, prices))
    if out is not sys.stdout:
        out.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dak-maint", description="DAK self-maintenance toolkit")
    sub = p.add_subparsers(dest="command", required=True)

    t = sub.add_parser("triage", help="依存更新を判定する")
    t.add_argument("--package", default=None, help="単一依存モード（--deps-json 未指定時は必須）")
    t.add_argument("--from", dest="from_version", default=None)
    t.add_argument("--to", dest="to_version", default=None)
    t.add_argument(
        "--deps-json", default=None,
        help='グループ化された Dependabot PR 用: JSON配列 [{"package","from","to"}, ...]',
    )
    t.add_argument("--ci-passed", default="false")
    t.add_argument("--changelog-file", default=None)
    t.add_argument("--fetch-changelog", action="store_true")
    t.add_argument("--assessor", choices=["heuristic", "llm"], default="heuristic")
    t.set_defaults(func=cmd_triage)

    w = sub.add_parser("watch", help="新技術を探索し提案する")
    w.add_argument("--charter", default="docs/CHARTER.md")
    w.add_argument("--existing-titles", default=None, help="JSON array of existing issue titles")
    w.add_argument("--max-items", type=int, default=2)
    w.set_defaults(func=cmd_watch)

    f = sub.add_parser("feature-sync", help="依存の新機能取り込みを提案する")
    f.add_argument("--deps", default=None, help='JSON: [{"package","from","to"}]')
    f.add_argument("--charter", default="docs/CHARTER.md")
    f.add_argument("--existing-titles", default=None)
    f.add_argument("--max-items", type=int, default=2)
    f.set_defaults(func=cmd_feature_sync)

    d = sub.add_parser("collect-deps", help="マージ済み deps PR の本文（stdin の JSON）から依存を集める")
    d.add_argument("--max-items", type=_non_negative_int, default=10)
    d.set_defaults(func=cmd_collect_deps)

    c = sub.add_parser("charter-review", help="憲章の見直しを提案する")
    c.add_argument("--charter", default="docs/CHARTER.md")
    c.add_argument("--quarter", default="")
    c.add_argument("--existing-titles", default=None)
    c.set_defaults(func=cmd_charter_review)

    m = sub.add_parser("compare-models", help="固定の入力で複数のモデルを比べる")
    m.add_argument("--models", required=True,
                   help='JSON ファイル: [{"name","base_url","model","api_key_env"}]（鍵は環境変数の名前だけ）')
    m.add_argument("--prices", default=None, help='JSON ファイル: {"<name>": {"input": 0.1, "output": 0.5}}（1M トークンあたりの USD）')
    m.add_argument("--cases", default=",".join(model_compare.CASES))
    m.add_argument("--out", default=None, help="Markdown の出力先（無ければ標準出力）")
    m.set_defaults(func=cmd_compare_models)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
