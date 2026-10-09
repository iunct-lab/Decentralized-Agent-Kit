"""capture-golden.yml must start Ollama the same way nightly-eval.yml does (#71).

Both workflows run the compose stack against Ollama on the runner. nightly-eval
learned that Ollama has to listen on 0.0.0.0 and be checked from a container
before the stack starts, and that llama3.2:3b never passes; capture-golden
drifted from both. This compares the two files so the drift fails a test.
Reads files only, so it does not need the Docker stack.
"""
import os
import re
import subprocess
from pathlib import Path

import pytest

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
STEP = "Install and start Ollama"


@pytest.fixture(scope="session", autouse=True)
def stack_ready():
    """Overrides conftest's stack wait: these tests only read files."""


def _lines(name: str) -> list[str]:
    return (WORKFLOWS / name).read_text().splitlines()


def _step(name: str, step: str = STEP) -> list[str]:
    """Lines of the named step, without its `- name:` line."""
    lines = _lines(name)
    start = next(i for i, line in enumerate(lines) if line.strip() == f"- name: {step}")
    indent = len(lines[start]) - len(lines[start].lstrip())
    body = []
    for line in lines[start + 1:]:
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        body.append(line)
    return body


def _run_block(name: str) -> list[str]:
    body = _step(name)
    start = next(i for i, line in enumerate(body) if line.strip() == "run: |")
    return body[start + 1:]


def _key(name: str, key: str, step: str = STEP) -> str | None:
    for line in _step(name, step):
        m = re.match(rf"\s*{key}:\s*(.+)$", line)
        if m:
            return m.group(1).strip()
    return None


def _model_default(name: str) -> str:
    lines = _lines(name)
    start = next(i for i, line in enumerate(lines) if line.strip() == "model:")
    for line in lines[start + 1:]:
        m = re.match(r'\s*default:\s*"?([^"]+)"?\s*$', line)
        if m:
            return m.group(1)
    raise AssertionError(f"{name}: no default for inputs.model")


def test_capture_golden_starts_ollama_like_nightly_eval():
    assert _run_block("capture-golden.yml") == _run_block("nightly-eval.yml")


def test_capture_golden_step_timeout_matches_nightly_eval():
    assert _key("capture-golden.yml", "timeout-minutes") == _key("nightly-eval.yml", "timeout-minutes")


def test_capture_golden_passes_its_model_input_to_the_step():
    assert _key("capture-golden.yml", "MODEL") == "${{ github.event.inputs.model }}"


def test_capture_golden_default_model_matches_nightly_eval():
    assert _model_default("capture-golden.yml") == _model_default("nightly-eval.yml")


def test_capture_golden_opens_pr_with_its_own_token():
    # A PR opened with GITHUB_TOKEN does not start CI, and the repository does not
    # let Actions create PRs (#498).
    token = _key("capture-golden.yml", "token", "Open PR with the new golden")
    assert token == "${{ secrets.GOLDEN_PR_TOKEN }}"


def test_capture_golden_token_permissions_are_read_only():
    text = (WORKFLOWS / "capture-golden.yml").read_text()
    assert "pull-requests: write" not in text
    assert "contents: write" not in text


def test_capture_golden_checks_the_token_before_installing_ollama():
    # A missing secret should fail in seconds, not after the ~15 min Ollama pull.
    steps = [line.strip() for line in _lines("capture-golden.yml") if line.strip().startswith("- name: ")]
    assert steps.index("- name: Check GOLDEN_PR_TOKEN") < steps.index(f"- name: {STEP}")
    step = "Check GOLDEN_PR_TOKEN"
    assert _key("capture-golden.yml", "GOLDEN_PR_TOKEN", step) == "${{ secrets.GOLDEN_PR_TOKEN }}"
    run = _key("capture-golden.yml", "run", step).strip("'")
    for token, code in (("", 1), ("set", 0)):
        env = {"PATH": os.environ["PATH"], "GOLDEN_PR_TOKEN": token}
        assert subprocess.run(["bash", "-c", run], env=env, capture_output=True).returncode == code
