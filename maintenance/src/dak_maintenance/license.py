"""依存のライセンスを方針（`maintenance/license-policy.toml`）と照らして判定する（PBI #344）。

入力は `pip-licenses --from=all --format=json` の一覧。欄は `License-Expression`（PEP 639）→
`License-Metadata` → `License-Classifier` の順に、SPDX の式にそろえられた最初のものを採る。
環境に入らない依存（Windows でだけ入るものなど）は、`uv export` の lock と突き合わせて PyPI の JSON から同じ形の行を作る。
方針と取り方の理由は `docs/maintenance/license-policy.md`。
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

import httpx

FIELDS = ("License-Expression", "License-Metadata", "License-Classifier")
_OPERATORS = {"AND", "OR", "WITH"}
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+-]*$")


@dataclass(frozen=True)
class Policy:
    allow: frozenset[str]
    aliases: dict[str, str]
    verified: dict[tuple[str, str], str]  # (パッケージ, 版) → LICENSE ファイルで確かめた SPDX の式
    exceptions: tuple[dict, ...]


@dataclass(frozen=True)
class Finding:
    component: str
    package: str
    version: str
    license: str
    status: str  # ok / exception / denied / unknown
    reason: str

    @property
    def failed(self) -> bool:
        return self.status in ("denied", "unknown")

    def message(self) -> str:
        if self.status == "unknown":
            return f"{self.component}: {self.package} {self.version} のライセンスが分からない（{self.reason}）"
        return f"{self.component}: {self.package} {self.version} のライセンス {self.license} は許容の一覧に無い（{self.reason}）"


def canonical_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def load_policy(path: str | Path) -> Policy:
    data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    return Policy(
        allow=frozenset(data.get("allow", [])),
        aliases=dict(data.get("aliases", {})),
        verified={(canonical_name(v["package"]), v["version"]): v["license"] for v in data.get("verified", [])},
        exceptions=tuple(data.get("exceptions", [])),
    )


def _tokens(expr: str) -> list[str]:
    return [t.upper() if t.upper() in _OPERATORS else t for t in re.findall(r"\(|\)|[^\s()]+", expr)]


class _Parser:
    """SPDX の式（`AND` / `OR` / `WITH` と括弧）を読み、許容の一覧と照らす。"""

    def __init__(self, tokens: list[str], allow: frozenset[str]):
        self.t, self.i, self.allow, self.denied = tokens, 0, allow, []

    def _peek(self) -> str | None:
        return self.t[self.i] if self.i < len(self.t) else None

    def _take(self) -> str:
        tok = self._peek()
        if tok is None:
            raise ValueError("式が途中で終わっている")
        self.i += 1
        return tok

    def parse(self) -> bool:
        ok = self._or()
        if self._peek() is not None:
            raise ValueError(f"読めない語: {self._peek()}")
        return ok

    def _or(self) -> bool:
        results = [self._and()]
        while self._peek() == "OR":
            self._take()
            results.append(self._and())
        return any(results)

    def _and(self) -> bool:
        results = [self._atom()]
        while self._peek() == "AND":
            self._take()
            results.append(self._atom())
        return all(results)

    def _atom(self) -> bool:
        tok = self._take()
        if tok == "(":
            ok = self._or()
            if self._take() != ")":
                raise ValueError("括弧が閉じていない")
            return ok
        if tok in _OPERATORS or tok == ")" or not _ID.match(tok):
            raise ValueError(f"ライセンスの ID ではない: {tok}")
        if self._peek() == "WITH":  # 例外の条項は許諾を足すだけなので、元のライセンスで判定する
            self._take()
            if not _ID.match(self._take()):
                raise ValueError("WITH の後が ID ではない")
        if tok in self.allow:
            return True
        self.denied.append(tok)
        return False


def evaluate_expression(expr: str, allow: frozenset[str] | set[str]) -> bool:
    """`OR` はどれか 1 つ、`AND` はすべてが許容なら真。読めない式は ValueError。"""
    return _Parser(_tokens(expr), frozenset(allow)).parse()


def _denied_ids(expr: str, allow: frozenset[str]) -> list[str]:
    p = _Parser(_tokens(expr), allow)
    p.parse()
    return p.denied


def normalize(raw: str | None, aliases: dict[str, str], known: frozenset[str] | None = None) -> str | None:
    """表記を SPDX の式にそろえる。そろえられなければ None（不明）。

    空・`UNKNOWN`・ライセンスの本文（改行を含む）は None。`; ` は pip-licenses が分類子をつないだもので、
    `AND` か `OR` か分からないので `AND` とみなす（緩い方に倒さない）。`known`（許容の一覧）を渡すと、版の数字の
    無い ID（`Apache`, `BSD`）はどのライセンスか決まらないので、`known` に無ければ None（自由記述の
    `License-Metadata` と分類子のため。`License-Expression` は PEP 639 で SPDX と決まっているので渡さない）。
    """
    s = (raw or "").strip()
    if not s or s == "UNKNOWN" or "\n" in s:
        return None
    if s in aliases:
        return aliases[s]
    if "; " in s:
        parts = [normalize(p, aliases, known) for p in s.split("; ")]
        if any(p is None for p in parts):
            return None
        return " AND ".join(f"({p})" if " " in p else p for p in parts)
    try:
        evaluate_expression(s, frozenset())
    except ValueError:
        return None
    toks = _tokens(s)
    ids = [t for i, t in enumerate(toks)
           if t not in _OPERATORS and t not in "()" and (i == 0 or toks[i - 1] != "WITH")]
    if known is not None and any(not re.search(r"\d", t) and t not in known for t in ids):
        return None
    return " ".join(_tokens(s)).replace("( ", "(").replace(" )", ")")


def _exception_for(policy: Policy, component: str, package: str) -> dict | None:
    for e in policy.exceptions:
        if canonical_name(e["package"]) == package and component in e.get("components", [component]):
            return e
    return None


def read_lock(text: str) -> list[tuple[str, str]]:
    """`uv export --format requirements-txt` の出力から (名前, 版) を読む。`-e .` と `--hash` の行は飛ばす。"""
    return [(m[1], m[2]) for line in text.splitlines()
            if (m := re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)", line))]


def fetch_pypi(name: str, version: str) -> dict:
    r = httpx.get(f"https://pypi.org/pypi/{name}/{version}/json", timeout=10.0)
    r.raise_for_status()
    return r.json()["info"]


def pypi_entry(name: str, version: str, info: dict) -> dict:
    """PyPI の JSON の `info` を pip-licenses の 3 つの欄の形に写す。"""
    classifiers = [c.split(" :: ")[-1] for c in info.get("classifiers") or [] if c.startswith("License ::")]
    return {"Name": name, "Version": version, "Source": "PyPI",
            "License-Expression": info.get("license_expression") or "UNKNOWN",
            "License-Metadata": info.get("license") or "UNKNOWN",
            "License-Classifier": "; ".join(classifiers) or "UNKNOWN"}


def add_lock_only(packages: list[dict], lock: list[tuple[str, str]],
                  fetch: Callable[[str, str], dict] | None = None) -> list[dict]:
    """lock にあって環境の一覧に無い (名前, 版) を、PyPI から取った行として足す。取れなければ不明にする。"""
    fetch = fetch or fetch_pypi
    installed = {(canonical_name(p["Name"]), p.get("Version", "")) for p in packages}
    added = []
    for name, version in lock:
        if (canonical_name(name), version) in installed:
            continue
        try:
            added.append(pypi_entry(name, version, fetch(name, version)))
        except (httpx.HTTPError, KeyError, ValueError) as e:
            added.append({"Name": name, "Version": version, "Source": "PyPI", "Error": repr(e)})
    return packages + added


def _judge(component: str, pkg: dict, policy: Policy) -> Finding:
    name, version = canonical_name(pkg["Name"]), pkg.get("Version", "")
    if pkg.get("Error"):  # PyPI に問い合わせられなかった行は、記録や例外より先に不明で止める
        return Finding(component, pkg["Name"], version, "不明", "unknown",
                       f"PyPI に問い合わせられない: {pkg['Error']}"[:200])
    expr, source = next(
        ((n, f) for f in FIELDS
         if (n := normalize(pkg.get(f), policy.aliases, None if f == FIELDS[0] else policy.allow))),
        (None, ""),
    )
    if (name, version) in policy.verified and source != FIELDS[0] and (
            expr is None or evaluate_expression(expr, policy.allow)):
        # 記録した版では、自由記述の欄が決まらないか許容のときだけ、LICENSE ファイルで確かめた式を採る
        # （分類子が PSF だけの pywin32 の BSD の部分を落とさない）。PEP 639 の式と、欄が言う許容外は隠さない
        expr, source = policy.verified[(name, version)], "方針の verified（LICENSE ファイルで確認）"
    exc = _exception_for(policy, component, name)
    if exc and expr is not None and expr != exc["license"] and not evaluate_expression(expr, policy.allow):
        # 例外は認めたときのライセンスに対してだけ。表記がそろえられて別の許容外のライセンスになったら止める
        return Finding(component, pkg["Name"], version, expr, "denied",
                       f"例外は {exc['license']} で認めたが、今は {expr}。{source}")
    if exc:
        return Finding(component, pkg["Name"], version, exc["license"], "exception",
                       f"{exc['reason']}（確認 {exc['reviewed']}）")
    if expr is None:
        raw = " / ".join(f"{f}={pkg.get(f, '')!r}"[:80] for f in FIELDS)
        return Finding(component, pkg["Name"], version, "不明", "unknown", raw)
    if evaluate_expression(expr, policy.allow):
        return Finding(component, pkg["Name"], version, expr, "ok", source)
    denied = ", ".join(_denied_ids(expr, policy.allow))
    return Finding(component, pkg["Name"], version, expr, "denied", f"{denied} が許容の一覧に無い。{source}")


def check(component: str, packages: list[dict], policy: Policy, exclude: set[str] = frozenset()) -> list[Finding]:
    """pip-licenses の一覧を判定する。`exclude` はコンポーネント自身のパッケージ名。"""
    excluded = {canonical_name(n) for n in exclude}
    findings = []
    for pkg in sorted(packages, key=lambda p: canonical_name(p["Name"])):
        if canonical_name(pkg["Name"]) in excluded:
            continue
        f = _judge(component, pkg, policy)
        if pkg.get("Source") == "PyPI":  # 環境に無く、PyPI の JSON から取った行。どの判定でも見分けられるように
            f = replace(f, reason=f"PyPI: {f.reason}")
        findings.append(f)
    return findings


def to_markdown(findings: list[Finding]) -> str:
    rows = ["| component | package | version | license | status | reason |", "|---|---|---|---|---|---|"]
    rows += [f"| {f.component} | {f.package} | {f.version} | {f.license} | {f.status} | {f.reason.replace('|', '/')} |"
             for f in findings]
    return "\n".join(rows) + "\n"
