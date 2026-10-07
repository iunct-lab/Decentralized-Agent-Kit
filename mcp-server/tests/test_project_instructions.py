import os
import sys
import tempfile
import unittest
from unittest.mock import patch

# Add parent directory to path to import main
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main


class TestProjectInstructions(unittest.IsolatedAsyncioTestCase):
    """get_project_instructions reads the instruction files from the workspace
    root (the server's working directory, /projects) down to `path`."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._cwd = os.getcwd()
        os.chdir(self._tmp.name)
        os.makedirs("sub/deeper")

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def _write(self, path, text):
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    async def test_concatenates_root_to_cwd_in_order(self):
        self._write("AGENTS.md", "root rules")
        self._write("sub/AGENTS.md", "sub rules")
        result = await main.get_project_instructions("sub/deeper")
        self.assertEqual(
            result,
            "--- ./AGENTS.md ---\nroot rules\n"
            "--- sub/AGENTS.md ---\nsub rules\n",
        )

    async def test_prefers_agents_md_over_claude_md_in_same_dir(self):
        self._write("AGENTS.md", "agents")
        self._write("CLAUDE.md", "claude")
        result = await main.get_project_instructions(".")
        self.assertIn("agents", result)
        self.assertNotIn("claude", result)

    async def test_prefers_claude_md_over_context_md_when_agents_md_absent(self):
        self._write("CLAUDE.md", "claude")
        self._write("CONTEXT.md", "context")
        result = await main.get_project_instructions(".")
        self.assertEqual(result, "--- ./CLAUDE.md ---\nclaude\n")

    async def test_skips_directories_without_any_instruction_file(self):
        self._write("sub/deeper/CONTEXT.md", "deep")
        result = await main.get_project_instructions("sub/deeper")
        self.assertEqual(result, "--- sub/deeper/CONTEXT.md ---\ndeep\n")

    async def test_file_path_reads_up_to_its_directory(self):
        self._write("sub/AGENTS.md", "sub rules")
        self._write("sub/deeper/AGENTS.md", "deep rules")
        self._write("sub/code.py", "")
        result = await main.get_project_instructions("sub/code.py")
        self.assertIn("sub rules", result)
        self.assertNotIn("deep rules", result)

    async def test_returns_empty_when_nothing_found(self):
        self.assertEqual(await main.get_project_instructions("sub"), "")

    async def test_truncates_when_over_max_bytes(self):
        self._write("AGENTS.md", "x" * 100)
        with patch.object(main, "MAX_INSTRUCTIONS_BYTES", 40):
            result = await main.get_project_instructions(".")
        body, marker = result.split("\n\n", 1)
        self.assertEqual(len(body), 40)
        self.assertEqual(marker, "[truncated: instructions exceeded MCP_INSTRUCTIONS_MAX_BYTES]")

    async def test_untrusted_path_returns_not_read_marker(self):
        os.makedirs("other")
        self._write("other/AGENTS.md", "untrusted rules")
        with patch.object(main, "TRUSTED_WORKSPACE_PREFIXES", ["sub"]):
            result = await main.get_project_instructions("other")
            trusted = await main.get_project_instructions("sub")
        self.assertTrue(result.startswith("[not read"))
        self.assertNotIn("untrusted rules", result)
        self.assertEqual(trusted, "")

    async def test_untrusted_prefix_does_not_read_trusted_root_either(self):
        self._write("AGENTS.md", "root rules")
        os.makedirs("subway")
        with patch.object(main, "TRUSTED_WORKSPACE_PREFIXES", ["sub"]):
            result = await main.get_project_instructions("subway")
        self.assertTrue(result.startswith("[not read"))

    async def test_path_outside_the_workspace_is_not_read(self):
        for path in ("..", "/etc", "sub/../../x"):
            result = await main.get_project_instructions(path)
            self.assertTrue(result.startswith("[not read"), path)

    async def test_symlink_leaving_the_workspace_is_not_read(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        self._write(os.path.join(outside.name, "AGENTS.md"), "outside rules")
        os.symlink(outside.name, "link")
        result = await main.get_project_instructions("link")
        self.assertTrue(result.startswith("[not read"))


if __name__ == "__main__":
    unittest.main()
