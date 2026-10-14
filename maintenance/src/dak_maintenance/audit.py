"""`uv audit`（preview）の出力から、ロックしている版の脆弱性の ID を取り出す。

読む欄は `vulnerabilities[].id` と `vulnerabilities[].dependency.{name,version}` だけ（preview で他の欄は変わりうる）。
脆弱性があると uv は終了コード 1 で終わる。0 件の `ids` と「audit できなかった」(`error`) は区別する。
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field

AUDIT_CMD = ["uv", "audit", "--frozen", "--preview-features", "audit-command,json-output",
             "--output-format", "json"]


@dataclass
class AuditResult:
    ids: list[str] = field(default_factory=list)
    packages: dict[str, list[str]] = field(default_factory=dict)  # "<name>==<version>" -> [id]
    error: str | None = None


def parse_audit_json(text: str) -> AuditResult:
    try:
        data = json.loads(text)
        vulns = data["vulnerabilities"]
        if not isinstance(vulns, list):
            raise TypeError("vulnerabilities is not a list")
        ids: list[str] = []
        packages: dict[str, list[str]] = {}
        for v in vulns:
            vid = v["id"]
            dep = v["dependency"]
            key = f"{dep['name']}=={dep['version']}"
            if vid not in ids:
                ids.append(vid)
            if vid not in packages.setdefault(key, []):
                packages[key].append(vid)
    except (ValueError, KeyError, TypeError) as e:
        return AuditResult(error=f"audit の出力を読めない: {e}")
    return AuditResult(ids=ids, packages=packages)


def audit_lock(project_dir: str) -> AuditResult:
    try:
        proc = subprocess.run(AUDIT_CMD, cwd=project_dir, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as e:
        return AuditResult(error=f"uv audit を実行できない: {e}")
    result = parse_audit_json(proc.stdout)
    if result.error:
        tail = (proc.stderr or "").strip().splitlines()[-1:] or [""]
        return AuditResult(error=f"{result.error}（終了コード {proc.returncode}: {tail[0][:200]}）")
    # 脆弱性があれば終了コード 1。ID が無いのに非 0 で終わったなら、脆弱性では説明できない失敗。
    if proc.returncode != 0 and not result.ids:
        tail = (proc.stderr or "").strip().splitlines()[-1:] or [""]
        return AuditResult(error=f"uv audit が終了コード {proc.returncode} で終わった: {tail[0][:200]}")
    return result
