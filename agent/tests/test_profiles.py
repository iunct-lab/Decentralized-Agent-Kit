"""Tests for harness profiles (dak_agent/profiles.py) and how HarnessSettings.from_env applies them."""
import os

import pytest

from dak_agent import profiles
from dak_agent.harness import HarnessSettings

BUNDLED_DIR = os.path.join(os.path.dirname(__file__), "..", "profiles")


def _write(dir_path, name, text):
    (dir_path / name).write_text(text, encoding="utf-8")


@pytest.fixture
def bundled():
    return profiles.load_profile_files([BUNDLED_DIR])


def test_glob_pattern_matches_model_name(bundled):
    assert profiles.resolve_profile("ollama_chat/qwen2.5:7b", None, bundled)["name"] == "qwen-small"
    # Anything else falls back to the catch-all default, whatever the file order.
    assert profiles.resolve_profile("ollama_chat/llama3.1:8b", None, bundled)["name"] == "default"


def test_no_match_returns_empty(tmp_path):
    _write(tmp_path, "qwen.yaml", 'pattern: "*qwen*"\n')
    loaded = profiles.load_profile_files([str(tmp_path), str(tmp_path / "missing")])
    assert profiles.resolve_profile("gemini/gemini-2.5-flash", None, loaded) == {}


def test_explicit_profile_overrides_glob(bundled):
    assert profiles.resolve_profile("ollama_chat/llama3.1:8b", "qwen-small", bundled)["name"] == "qwen-small"


def test_missing_explicit_profile_raises(bundled):
    with pytest.raises(ValueError, match="no-such-profile"):
        profiles.resolve_profile("ollama_chat/qwen2.5:7b", "no-such-profile", bundled)


def test_profile_without_pattern_raises(tmp_path):
    _write(tmp_path, "broken.yaml", "tool_output_max_chars: 100\n")
    with pytest.raises(ValueError, match="pattern"):
        profiles.load_profile_files([str(tmp_path)])


def test_from_env_applies_profile(monkeypatch):
    monkeypatch.delenv("DAK_HARNESS_PROFILE", raising=False)
    monkeypatch.delenv("DAK_TOOL_OUTPUT_MAX_CHARS", raising=False)
    settings = HarnessSettings.from_env("ollama_chat/qwen2.5:7b")
    assert settings.tool_output_max_chars == 8000
    assert settings.tool_allowlist == (
        "read_file", "write_file", "list_files", "search_files", "switch_mode", "attempt_answer",
    )
    assert settings.max_tools == 8


def test_from_env_default_profile_keeps_current_defaults(monkeypatch):
    monkeypatch.delenv("DAK_HARNESS_PROFILE", raising=False)
    monkeypatch.delenv("DAK_TOOL_OUTPUT_MAX_CHARS", raising=False)
    settings = HarnessSettings.from_env("ollama_chat/llama3.1:8b")
    assert settings == HarnessSettings(context_window=settings.context_window)


def test_explicit_profile_env_selects_profile(monkeypatch):
    monkeypatch.setenv("DAK_HARNESS_PROFILE", "qwen-small")
    monkeypatch.delenv("DAK_TOOL_OUTPUT_MAX_CHARS", raising=False)
    assert HarnessSettings.from_env("ollama_chat/llama3.1:8b").max_tools == 8


def test_env_var_still_overrides_profile_value(monkeypatch):
    monkeypatch.delenv("DAK_HARNESS_PROFILE", raising=False)
    monkeypatch.setenv("DAK_TOOL_OUTPUT_MAX_CHARS", "1234")
    assert HarnessSettings.from_env("ollama_chat/qwen2.5:7b").tool_output_max_chars == 1234
