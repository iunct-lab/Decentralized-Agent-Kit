import json
from pathlib import Path

import httpx
import pytest

from dak_maintenance.cli import main
from dak_maintenance.license import (
    Policy, add_lock_only, check, evaluate_expression, load_policy, normalize, pypi_entry, read_lock,
)

REPO_POLICY = Path(__file__).resolve().parents[1] / "license-policy.toml"

POLICY_TOML = """
allow = ["MIT", "Apache-2.0", "BSD-3-Clause", "MPL-2.0"]

[aliases]
"MIT License" = "MIT"
"Apache Software License" = "Apache-2.0"

[[verified]]
package = "Jinja2"
version = "3.1.6"
license = "BSD-3-Clause"
reviewed = "2026-10-01"

[[exceptions]]
package = "psycopg2-binary"
license = "LGPL-3.0-or-later"
components = ["agent"]
reason = "動的に読み込むだけ"
reviewed = "2026-10-07"
"""


def _pkg(name, expression="UNKNOWN", metadata="UNKNOWN", classifier="UNKNOWN", version="1.0"):
    return {"Name": name, "Version": version, "License-Expression": expression,
            "License-Metadata": metadata, "License-Classifier": classifier, "URL": "UNKNOWN"}


@pytest.fixture
def policy(tmp_path):
    path = tmp_path / "policy.toml"
    path.write_text(POLICY_TOML, encoding="utf-8")
    return load_policy(path)


ALIASES = {"MIT License": "MIT", "Apache Software License": "Apache-2.0"}


@pytest.mark.parametrize("raw, expected", [
    ("MIT License", "MIT"),
    ("  MIT\n", "MIT"),
    ("Apache-2.0 or BSD-3-Clause", "Apache-2.0 OR BSD-3-Clause"),
    ("Apache Software License; MIT License", "Apache-2.0 AND MIT"),
    ("(MIT OR Apache-2.0) AND BSD-3-Clause", "(MIT OR Apache-2.0) AND BSD-3-Clause"),
    ("UNKNOWN", None),
    ("", None),
    (None, None),
    ("MIT License\n\nPermission is hereby granted ...", None),
    ("BSD License", None),
    ("Apache Software License; BSD License", None),
    ("Apache", None),
    ("BSD", None),
    ("GPL-3.0-only", "GPL-3.0-only"),
])
def test_normalize(raw, expected):
    assert normalize(raw, ALIASES, frozenset({"MIT"})) == expected


@pytest.mark.parametrize("expr, expected", [
    ("MIT", True),
    ("GPL-3.0-only", False),
    ("MIT OR GPL-3.0-only", True),
    ("MIT AND GPL-3.0-only", False),
    ("MIT AND Apache-2.0", True),
    ("(MIT OR GPL-3.0-only) AND Apache-2.0", True),
    ("GPL-3.0-only OR (MIT AND LGPL-2.1-only)", False),
    ("Apache-2.0 WITH LLVM-exception", True),
])
def test_evaluate_expression(expr, expected):
    assert evaluate_expression(expr, {"MIT", "Apache-2.0"}) is expected


@pytest.mark.parametrize("expr", ["MIT AND", "(MIT", "MIT Apache-2.0", "MIT License"])
def test_evaluate_expression_rejects_malformed(expr):
    with pytest.raises(ValueError):
        evaluate_expression(expr, {"MIT"})


def test_fields_are_taken_expression_then_metadata_then_classifier(policy):
    [a, b, c] = check("agent", [
        _pkg("a", expression="MIT", metadata="GPL-3.0-only"),
        _pkg("b", metadata="BSD-3-Clause", classifier="BSD License"),
        _pkg("c", metadata="Apache", classifier="Apache Software License"),
    ], policy)
    assert (a.license, a.status, a.reason) == ("MIT", "ok", "License-Expression")
    assert (b.license, b.reason) == ("BSD-3-Clause", "License-Metadata")
    assert (c.license, c.reason) == ("Apache-2.0", "License-Classifier")


def test_verified_record_resolves_an_ambiguous_label(policy):
    [f] = check("bff", [_pkg("jinja2", classifier="BSD License", version="3.1.6")], policy)
    assert (f.license, f.status) == ("BSD-3-Clause", "ok")


def test_verified_record_does_not_hide_a_new_version_or_a_declared_license(policy):
    [newer] = check("bff", [_pkg("Jinja2", classifier="BSD License", version="3.2.0")], policy)
    [declared] = check("bff", [_pkg("Jinja2", expression="GPL-3.0-only", version="3.1.6")], policy)
    assert newer.status == "unknown"
    assert (declared.status, declared.license) == ("denied", "GPL-3.0-only")


def test_exception_does_not_cover_a_different_license(policy):
    [f] = check("agent", [_pkg("psycopg2-binary", expression="AGPL-3.0-only")], policy)
    assert f.status == "denied" and "LGPL-3.0-or-later で認めた" in f.reason


def test_with_exception_id_without_a_digit_goes_through_check(policy):
    [f] = check("agent", [_pkg("llvmish", expression="Apache-2.0 WITH LLVM-exception")], policy)
    assert (f.status, f.license) == ("ok", "Apache-2.0 WITH LLVM-exception")


def test_digitless_ids_are_trusted_in_license_expression(policy):
    [f] = check("agent", [_pkg("z", expression="Zlib AND MIT", metadata="MIT")], policy)
    assert (f.status, f.license) == ("denied", "Zlib AND MIT")


def test_exception_applies_only_inside_its_components(policy):
    pkg = _pkg("psycopg2-binary", metadata="LGPL with exceptions",
               classifier="GNU Library or Lesser General Public License (LGPL)")
    [inside] = check("agent", [pkg], policy)
    [outside] = check("cli", [pkg], policy)
    assert (inside.status, inside.license) == ("exception", "LGPL-3.0-or-later")
    assert "動的に読み込むだけ" in inside.reason and "2026-10-07" in inside.reason
    assert outside.status == "unknown"


def test_missing_metadata_is_unknown(policy):
    [f] = check("agent", [_pkg("jsonalias", version="0.1.1")], policy)
    assert (f.status, f.license) == ("unknown", "不明")
    assert f.message().startswith("agent: jsonalias 0.1.1 のライセンスが分からない")


def test_denied_names_the_license_that_is_not_allowed(policy):
    [f] = check("cli", [_pkg("gplpkg", expression="MIT AND GPL-3.0-only", version="2.0")], policy)
    assert f.status == "denied"
    assert f.message() == ("cli: gplpkg 2.0 のライセンス MIT AND GPL-3.0-only は許容の一覧に無い"
                           "（GPL-3.0-only が許容の一覧に無い。License-Expression）")


def test_component_itself_is_excluded(policy):
    assert check("agent", [_pkg("dak_agent")], policy, exclude={"dak-agent"}) == []


def test_cli_exit_code_messages_and_markdown(policy, tmp_path, capsys):
    (tmp_path / "policy.toml").write_text(POLICY_TOML, encoding="utf-8")
    listing = tmp_path / "cli.json"
    listing.write_text(json.dumps([_pkg("rich", metadata="MIT"), _pkg("gplpkg", expression="GPL-3.0-only")]))
    out = tmp_path / "report" / "cli.md"
    args = ["license-check", "--component", "cli", "--input", str(listing),
            "--policy", str(tmp_path / "policy.toml"), "--markdown-out", str(out)]
    assert main(args) == 1
    stdout = capsys.readouterr().out
    assert "cli: gplpkg 1.0 のライセンス GPL-3.0-only は許容の一覧に無い" in stdout
    assert "rich" not in stdout
    table = out.read_text(encoding="utf-8")
    assert "| cli | rich | 1.0 | MIT | ok |" in table and "| cli | gplpkg |" in table

    listing.write_text(json.dumps([_pkg("rich", metadata="MIT")]))
    assert main(args) == 0


def test_cli_malformed_policy_value_is_exit_2(tmp_path):
    (tmp_path / "policy.toml").write_text('allow = ["MIT"]\n[aliases]\n"Foo" = "Apache 2.0"\n', encoding="utf-8")
    (tmp_path / "in.json").write_text(json.dumps([_pkg("x", metadata="Foo")]))
    assert main(["license-check", "--component", "cli", "--input", str(tmp_path / "in.json"),
                 "--policy", str(tmp_path / "policy.toml")]) == 2


def test_cli_unreadable_input_is_exit_2(tmp_path):
    assert main(["license-check", "--component", "cli", "--input", str(tmp_path / "none.json"),
                 "--policy", str(REPO_POLICY)]) == 2


def test_repository_policy_matches_the_users_decision():
    p: Policy = load_policy(REPO_POLICY)
    assert "MPL-2.0" in p.allow and not any(a.startswith("LGPL") for a in p.allow)
    assert {(e["package"], tuple(e["components"])) for e in p.exceptions} == {
        ("psycopg2-binary", ("agent",)), ("jsonalias", ("agent",)),
    }
    assert all(evaluate_expression(v, p.allow) for v in p.verified.values())


LOCK = """# This file was autogenerated by uv via the following command:
#    uv export --frozen --no-dev --format requirements-txt
-e .
click==8.3.1 ; python_full_version >= '3.10' \\
    --hash=sha256:aaaa
colorama==0.4.6 ; sys_platform == 'win32' \\
    --hash=sha256:bbbb
rich==14.0.0
"""

COLORAMA_INFO = {"license": "", "license_expression": None,
                 "classifiers": ["Intended Audience :: Developers", "License :: OSI Approved :: BSD License"]}


def test_read_lock_skips_editable_and_hash_lines():
    assert read_lock(LOCK) == [("click", "8.3.1"), ("colorama", "0.4.6"), ("rich", "14.0.0")]


def test_pypi_entry_maps_info_to_pip_licenses_fields():
    assert pypi_entry("tzdata", "2025.2", {"license": "Apache-2.0", "classifiers": []}) == {
        "Name": "tzdata", "Version": "2025.2", "Source": "PyPI", "License-Expression": "UNKNOWN",
        "License-Metadata": "Apache-2.0", "License-Classifier": "UNKNOWN",
    }
    assert pypi_entry("colorama", "0.4.6", COLORAMA_INFO)["License-Classifier"] == "BSD License"


def test_only_lock_entries_missing_from_the_environment_are_fetched(policy):
    asked = []

    def fetch(name, version):
        asked.append((name, version))
        return {"license_expression": "MIT"}

    env = [_pkg("click", metadata="BSD-3-Clause", version="8.3.1"), _pkg("Rich", metadata="MIT", version="14.0.0")]
    findings = check("cli", add_lock_only(env, read_lock(LOCK), fetch), policy)
    assert asked == [("colorama", "0.4.6")]
    [colorama] = [f for f in findings if f.package == "colorama"]
    assert (colorama.status, colorama.license, colorama.reason) == ("ok", "MIT", "PyPI License-Expression")


def test_lock_only_entry_without_a_clear_license_is_unknown(policy):
    [f] = check("cli", add_lock_only([], [("colorama", "0.4.6")], lambda n, v: COLORAMA_INFO), policy)
    assert f.status == "unknown"


def test_pypi_failure_is_unknown_with_the_error(policy):
    def fetch(name, version):
        raise httpx.HTTPStatusError("404 Not Found", request=httpx.Request("GET", "https://pypi.org"),
                                    response=httpx.Response(404))

    [f] = check("cli", add_lock_only([], [("gone", "1.0")], fetch), policy)
    assert f.status == "unknown" and "PyPI に問い合わせられない" in f.reason and "404" in f.reason


def test_repository_policy_resolves_colorama_from_pypi():
    [f] = check("cli", add_lock_only([], [("colorama", "0.4.6")], lambda n, v: COLORAMA_INFO), load_policy(REPO_POLICY))
    assert (f.status, f.license) == ("ok", "BSD-3-Clause")
