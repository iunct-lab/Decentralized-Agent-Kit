"""CLI E2E: dak-cli run against the test agent."""
import json
import os
import subprocess

import pytest

from conftest import AGENT_URL

MODEL = "fake-default"

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CLI_DIR = os.path.join(REPO_ROOT, "cli")


@pytest.mark.skipif(not os.path.isdir(CLI_DIR), reason="cli directory not found")
def test_cli_run_round_trip(fake_llm, tmp_path):
    fake_llm.clear(MODEL)
    fake_llm.script(MODEL, [fake_llm.text("CLI round trip answer.")])

    # Isolate the CLI config (~/.dak-cli) so we don't touch the real one
    env = dict(os.environ)
    env["HOME"] = str(tmp_path)
    env["DAK_AGENT_URL"] = AGENT_URL

    config_dir = tmp_path / ".dak-cli"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(json.dumps({"username": "it_cli_user"}))

    result = subprocess.run(
        ["uv", "run", "dak-cli", "run", "Hello via CLI"],
        cwd=CLI_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
    assert "CLI round trip answer." in result.stdout


MODE_SWITCH_PATH = "tests/integration/artifacts/dak_cli_mode_switch_test.txt"


@pytest.mark.skipif(not os.path.isdir(CLI_DIR), reason="cli directory not found")
def test_cli_mode_switch_preserves_confirmation_requirement(fake_llm, tmp_path):
    """PBI #21: the tools a mode switch rebuilds ask for confirmation the same
    way (agent's permission rules), so `dak-cli run` still asks before write_file."""
    written = os.path.join(REPO_ROOT, MODE_SWITCH_PATH)
    fake_llm.clear(MODEL)
    fake_llm.script(MODEL, [
        fake_llm.tool_call("switch_mode", reason="need file tools", new_focus="write a file"),
        # mode_manager's meta call reads the same scripted model
        fake_llm.text(json.dumps({"instruction": "Write the requested file.",
                                  "selected_tools": ["write_file", "switch_mode"], "selected_skills": []})),
        fake_llm.tool_call("write_file", path=MODE_SWITCH_PATH, content="hello"),
        fake_llm.text("Waiting for approval."),  # the call is held; the model gets one more step
        fake_llm.text("Done, file written."),
    ])

    env = dict(os.environ)
    env["HOME"] = str(tmp_path)
    env["DAK_AGENT_URL"] = AGENT_URL
    config_dir = tmp_path / ".dak-cli"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(json.dumps({"username": "it_cli_user"}))

    try:
        result = subprocess.run(
            ["uv", "run", "dak-cli", "run", "Please write hello into a file."],
            cwd=CLI_DIR,
            env=env,
            input="y\n",  # answer typer.confirm: approve
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
        assert "Approval Required" in result.stdout
        assert "write_file" in result.stdout
        assert "Done, file written." in result.stdout
        assert os.path.exists(written)
    finally:
        if os.path.exists(written):
            os.remove(written)
