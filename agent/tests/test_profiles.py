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


# --- PBI #750: profile `instruction` -> startup base instruction ---

def _clear_instruction_env(monkeypatch):
    monkeypatch.delenv("AGENT_INSTRUCTION", raising=False)
    monkeypatch.delenv("DAK_HARNESS_PROFILE", raising=False)


def test_base_instruction_uses_profile_instruction(monkeypatch):
    from dak_agent.agent import base_instruction
    from dak_agent.explorer import DELEGATION_INSTRUCTION

    _clear_instruction_env(monkeypatch)
    assert base_instruction(False, "Short.") == "Short.\n\n" + DELEGATION_INSTRUCTION


def test_agent_instruction_env_wins_over_profile(monkeypatch):
    from dak_agent.agent import base_instruction

    monkeypatch.setenv("AGENT_INSTRUCTION", "From env.")
    assert base_instruction(False, "Short.").startswith("From env.\n\n")


def test_enforcer_instruction_wins_over_profile(monkeypatch):
    from dak_agent.agent import base_instruction
    from dak_agent.enforcer import ENFORCER_INSTRUCTION

    monkeypatch.setenv("AGENT_INSTRUCTION", "From env.")
    assert base_instruction(True, "Short.").startswith(ENFORCER_INSTRUCTION)


def test_no_profile_instruction_keeps_the_default(monkeypatch):
    from dak_agent.agent import base_instruction
    from dak_agent.explorer import DELEGATION_INSTRUCTION

    _clear_instruction_env(monkeypatch)
    assert base_instruction(False, None) == (
        "You are a helpful assistant powered by the Decentralized Agent Kit.\n\n" + DELEGATION_INSTRUCTION
    )


def test_from_env_reads_profile_instruction(monkeypatch, tmp_path):
    _clear_instruction_env(monkeypatch)
    _write(tmp_path, "short.yaml", 'pattern: "*"\ninstruction: "Short."\n')
    real = profiles.load_profile_files
    monkeypatch.setattr(profiles, "load_profile_files", lambda dirs: real([str(tmp_path)]))
    assert HarnessSettings.from_env("ollama_chat/llama3.1:8b").instruction == "Short."


def test_bundled_profiles_have_no_instruction(monkeypatch):
    _clear_instruction_env(monkeypatch)
    for model in ("ollama_chat/llama3.1:8b", "ollama_chat/qwen2.5:7b"):
        assert HarnessSettings.from_env(model).instruction is None


def _profile_app(llm):
    from unittest.mock import MagicMock

    from google.adk.apps import App

    from dak_agent.adaptive_agent import AdaptiveAgent
    from dak_agent.agent import base_instruction
    from dak_agent.skill_registry import SkillRegistry

    agent = AdaptiveAgent(model=llm, name="dak_agent", instruction=base_instruction(False, "Short."), tools=[])
    agent.skill_registry = MagicMock(spec=SkillRegistry)
    agent.skill_registry.get_skill.return_value = None
    agent.skill_registry.list_skills.return_value = []
    return App(name="dak_agent", root_agent=agent)


async def _profile_request(monkeypatch, state_delta=None):
    import test_call_scoped_instruction as t
    from google.adk.sessions import InMemorySessionService

    _clear_instruction_env(monkeypatch)
    llm, requests = t._recording_llm()
    sessions = InMemorySessionService()
    session = await sessions.create_session(app_name="dak_agent", user_id="u")
    await t._run(_profile_app(llm), sessions, session.id, state_delta=state_delta)
    return t._system(requests[0])


@pytest.fixture(autouse=False)
def _no_remote(monkeypatch):
    from unittest.mock import AsyncMock, patch

    with patch("dak_agent.remote_tools.discover_remote_tools", AsyncMock(return_value={})):
        yield


@pytest.mark.asyncio
async def test_profile_instruction_reaches_the_model(monkeypatch, _no_remote):
    assert (await _profile_request(monkeypatch)).startswith("Short.")


@pytest.mark.asyncio
async def test_call_instruction_replaces_profile_instruction(monkeypatch, _no_remote):
    system = await _profile_request(monkeypatch, {"dak:instruction": "One word."})
    assert "One word." in system and "Short." not in system


@pytest.mark.asyncio
async def test_mode_instruction_replaces_profile_instruction(monkeypatch, _no_remote):
    from dak_agent.skill_tools import STATE_MODE_INSTRUCTION

    system = await _profile_request(monkeypatch, {STATE_MODE_INSTRUCTION: "Mode."})
    assert "Mode." in system and "Short." not in system
