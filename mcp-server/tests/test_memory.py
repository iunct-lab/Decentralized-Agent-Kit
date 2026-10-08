import os
import sys
import tempfile
import unittest
from unittest.mock import patch

# Add parent directory to path to import main
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main


class TestMemory(unittest.IsolatedAsyncioTestCase):
    """save_memory appends to one file per scope under the workspace root (the
    server's working directory, /projects); load_memory reads it back capped."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._cwd = os.getcwd()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()

    async def test_save_and_load_round_trip_for_project_scope(self):
        result = await main.save_memory("tests run with uv run pytest -q", scope="project")
        self.assertIn("Saved to project memory", result)
        self.assertTrue(os.path.isfile(".dak/memory/project.md"))
        await main.save_memory("the BFF listens on 8002")
        loaded = await main.load_memory("project")
        self.assertIn("tests run with uv run pytest -q", loaded)
        self.assertIn("the BFF listens on 8002", loaded)

    async def test_user_and_project_scopes_do_not_mix(self):
        await main.save_memory("prefers concise answers", scope="user")
        await main.save_memory("uses Conventional Commits", scope="project")
        user = await main.load_memory("user")
        project = await main.load_memory("project")
        self.assertIn("prefers concise answers", user)
        self.assertNotIn("uses Conventional Commits", user)
        self.assertIn("uses Conventional Commits", project)
        self.assertNotIn("prefers concise answers", project)

    async def test_invalid_scope_returns_error_for_save_and_load(self):
        self.assertTrue((await main.save_memory("x", scope="team")).startswith("Error"))
        self.assertTrue((await main.load_memory("../user")).startswith("Error"))
        self.assertFalse(os.path.exists(".dak"))

    async def test_load_missing_scope_returns_empty_string(self):
        self.assertEqual(await main.load_memory("user"), "")
        self.assertEqual(await main.load_memory(""), "")

    async def test_load_memory_caps_oversized_single_scope(self):
        await main.save_memory("OLDEST " + "a" * 300)
        await main.save_memory("NEWEST fact")
        with patch.object(main, "MEMORY_MAX_CHARS", 200):
            loaded = await main.load_memory("project")
        # The newest entries are kept; the older part is left in the file.
        self.assertIn("NEWEST", loaded)
        self.assertNotIn("OLDEST", loaded)
        self.assertIn("[truncated:", loaded)
        self.assertIn(".dak/memory/project.md", loaded)
        with open(".dak/memory/project.md", encoding="utf-8") as f:
            self.assertIn("OLDEST", f.read())

    async def test_load_memory_both_scopes_combined_within_bound(self):
        await main.save_memory("u" * 10000, scope="user")
        await main.save_memory("p" * 10000, scope="project")
        with patch.object(main, "MEMORY_MAX_CHARS", 4000):
            loaded = await main.load_memory("")
        self.assertIn("# User Memory", loaded)
        self.assertIn("# Project Memory", loaded)
        self.assertIn("u" * 100, loaded)
        self.assertIn("p" * 100, loaded)
        # Each scope gets half of the limit; the headings and truncation notes add a little.
        self.assertLess(len(loaded), 4000 * 1.2)


if __name__ == "__main__":
    unittest.main()
