import os
import re
import tempfile
import unittest
import unittest.mock

from dak_agent import config as config_module
from dak_agent.config import (
    DEFAULT_MODEL_NAME,
    AgentConfig,
    get_litellm_model_name,
    load_agent_config,
    resolve_model_name,
)

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


class TestLoadAgentConfig(unittest.TestCase):
    def _write_config(self, content: str) -> str:
        f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        f.write(content)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_load_full_config(self):
        path = self._write_config(
            """
mcp_servers:
  - name: "local-mcp"
    url: "http://mcp-server:8000/mcp"
    type: "http"
  - name: "extra-mcp"
    url: "http://extra:8000/sse"
    type: "sse"

a2a_peers:
  - name: "agent_provider"
    url: "http://agent-provider:8000"
    capabilities: ["premium_service"]
"""
        )
        config = load_agent_config(path)
        self.assertEqual(set(config.mcp_servers), {"local-mcp", "extra-mcp"})
        self.assertEqual(config.mcp_servers["extra-mcp"]["type"], "sse")
        self.assertEqual(len(config.a2a_peers), 1)
        self.assertEqual(config.a2a_peers[0]["name"], "agent_provider")

    def test_missing_file_returns_empty_config(self):
        config = load_agent_config("/nonexistent/agent_config.yaml")
        self.assertEqual(config, AgentConfig())

    def test_malformed_entries_are_skipped(self):
        path = self._write_config(
            """
mcp_servers:
  - url: "http://no-name:8000"
  - name: "good"
    url: "http://good:8000/mcp"
"""
        )
        config = load_agent_config(path)
        self.assertEqual(set(config.mcp_servers), {"good"})

    def test_empty_file(self):
        path = self._write_config("")
        config = load_agent_config(path)
        self.assertEqual(config, AgentConfig())


class TestLitellmModelName(unittest.TestCase):
    def test_gemini_gets_prefixed(self):
        self.assertEqual(get_litellm_model_name("gemini-2.5-flash"), "gemini/gemini-2.5-flash")

    def test_prefixed_gemini_unchanged(self):
        self.assertEqual(get_litellm_model_name("gemini/gemini-2.5-flash"), "gemini/gemini-2.5-flash")

    def test_other_models_unchanged(self):
        self.assertEqual(get_litellm_model_name("openai/gpt-4o"), "openai/gpt-4o")
        self.assertEqual(get_litellm_model_name("claude-3-opus"), "claude-3-opus")


class TestResolveModelName(unittest.TestCase):
    def test_model_name_wins(self):
        env = {"MODEL_NAME": "openai/llamacpp", "GEMINI_MODEL_NAME": "gemini-x"}
        self.assertEqual(resolve_model_name(env), "openai/llamacpp")

    def test_gemini_model_name_is_ignored(self):
        """The old GEMINI_MODEL_NAME alias is gone; only MODEL_NAME picks the model."""
        self.assertEqual(resolve_model_name({"GEMINI_MODEL_NAME": "gemini-x"}), DEFAULT_MODEL_NAME)

    def test_unset_falls_back_to_default(self):
        self.assertEqual(resolve_model_name({}), DEFAULT_MODEL_NAME)

    def test_blank_counts_as_unset(self):
        """`MODEL_NAME=` in .env, or an empty shell export, arrives as ""."""
        self.assertEqual(resolve_model_name({"MODEL_NAME": ""}), DEFAULT_MODEL_NAME)
        self.assertEqual(resolve_model_name({"MODEL_NAME": "  "}), DEFAULT_MODEL_NAME)
        self.assertEqual(
            resolve_model_name({"MODEL_NAME": "", "GEMINI_MODEL_NAME": "gemini-x"}), DEFAULT_MODEL_NAME
        )

    def test_reads_process_env_by_default(self):
        with unittest.mock.patch.dict(os.environ, {"MODEL_NAME": "openai/fake-default"}):
            self.assertEqual(resolve_model_name(), "openai/fake-default")


class TestDefaultModelSingleSource(unittest.TestCase):
    """DEFAULT_MODEL_NAME lives in config.py only; repo-level files must not re-spell it.

    Two checks read repo-root files (docker-compose.yml, .env.example). CI and
    a normal checkout always have them; they skip only when agent/ is tested
    on its own (e.g. copied out of the monorepo).
    """

    def _read(self, relpath: str) -> str:
        path = os.path.join(_REPO_ROOT, relpath)
        if not os.path.exists(path):
            self.skipTest(f"{relpath} not available (not a repo checkout)")
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_compose_does_not_repeat_the_default(self):
        compose = self._read("docker-compose.yml")
        # Positive form: the agent's MODEL_NAME entry must be the bare
        # pass-through. Any `MODEL_NAME=...` / `MODEL_NAME: ...` spelling
        # (whatever the default syntax or model) reintroduces a second default.
        self.assertRegex(compose, r"(?m)^\s*-\s*MODEL_NAME\s*$")
        self.assertNotRegex(compose, r"(?m)^\s*-?\s*MODEL_NAME\s*[=:]")
        self.assertNotIn(DEFAULT_MODEL_NAME, compose)

    def test_env_example_sample_matches_the_default(self):
        """.env.example is the one human-facing place that names the default."""
        match = re.search(r"^MODEL_NAME=(\S+)", self._read(".env.example"), re.MULTILINE)
        self.assertIsNotNone(match, ".env.example should carry a MODEL_NAME sample line")
        self.assertEqual(match.group(1), DEFAULT_MODEL_NAME)

    def test_no_other_module_spells_the_default(self):
        pkg = os.path.dirname(os.path.abspath(config_module.__file__))
        offenders, scanned = [], 0
        for root, _dirs, files in os.walk(pkg):
            for name in files:
                if not name.endswith(".py") or name == "config.py":
                    continue
                scanned += 1
                path = os.path.join(root, name)
                with open(path, encoding="utf-8") as f:
                    if DEFAULT_MODEL_NAME in f.read():
                        offenders.append(os.path.relpath(path, pkg))
        self.assertGreater(scanned, 5, f"expected to scan the dak_agent package, got {pkg}")
        self.assertEqual(offenders, [], "import DEFAULT_MODEL_NAME from dak_agent.config instead")


class TestNoDeadLocalLlmEnvVars(unittest.TestCase):
    """LOCAL_LLM_BASE_URL / LOCAL_LLM_API_KEY were documented but never read.

    The local LLM path is MODEL_NAME=ollama_chat/... plus OLLAMA_API_BASE, so
    the dead pair must not come back into the files users copy settings from.
    """

    def test_local_llm_base_url_not_present(self):
        for relpath in ("docker-compose.yml", ".env.example", "README.md"):
            path = os.path.join(_REPO_ROOT, relpath)
            if not os.path.exists(path):
                self.skipTest(f"{relpath} not available (not a repo checkout)")
            with open(path, encoding="utf-8") as f:
                text = f.read()
            for name in ("LOCAL_LLM_BASE_URL", "LOCAL_LLM_API_KEY"):
                self.assertNotIn(name, text, f"{relpath} still mentions the unused {name}")


if __name__ == "__main__":
    unittest.main()
