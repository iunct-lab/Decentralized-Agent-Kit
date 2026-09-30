import unittest
from unittest.mock import patch, mock_open, MagicMock, AsyncMock
import os
import sys
import subprocess
from types import SimpleNamespace

# Add parent directory to path to import main
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main
from sandbox import SandboxManager


class TestMCPTools(unittest.IsolatedAsyncioTestCase):
    """Test suite for MCP server tools."""

    async def test_deep_think(self):
        """Test deep_think tool returns the thought."""
        thought = "This is a deep thought"
        result = await main.deep_think(thought)
        self.assertEqual(result, thought)

    async def test_read_file_success(self):
        """Test read_file successfully reads file content."""
        test_content = "File content here"
        
        with patch('builtins.open', mock_open(read_data=test_content)):
            result = await main.read_file("/test/path.txt")
            self.assertEqual(result, test_content)

    async def test_read_file_error(self):
        """Test read_file handles errors gracefully."""
        with patch('builtins.open', side_effect=FileNotFoundError("File not found")):
            result = await main.read_file("/nonexistent/path.txt")
            self.assertIn("Error reading file", result)

    async def test_write_file_success(self):
        """Test write_file successfully writes content."""
        test_content = "Test content"
        test_path = "/test/path.txt"
        
        with patch('builtins.open', mock_open()) as mock_file:
            with patch('os.makedirs') as mock_makedirs:
                result = await main.write_file(test_path, test_content)
                
                mock_makedirs.assert_called_once()
                mock_file.assert_called_once_with(test_path, "w", encoding="utf-8")
                self.assertIn("Successfully wrote", result)

    async def test_write_file_error(self):
        """Test write_file handles errors gracefully."""
        with patch('builtins.open', side_effect=PermissionError("Permission denied")):
            result = await main.write_file("/test/path.txt", "content")
            self.assertIn("Error writing file", result)

    async def test_list_files_success(self):
        """Test list_files returns directory contents."""
        mock_items = ["file1.txt", "file2.py", "dir1"]
        
        with patch('os.listdir', return_value=mock_items):
            result = await main.list_files("/test/dir")
            
            for item in mock_items:
                self.assertIn(item, result)

    async def test_list_files_error(self):
        """Test list_files handles errors gracefully."""
        with patch('os.listdir', side_effect=FileNotFoundError("Directory not found")):
            result = await main.list_files("/nonexistent/dir")
            self.assertIn("Error listing files", result)

    async def test_run_command_success(self):
        """Test run_command executes commands successfully."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Command output"
        mock_result.stderr = ""
        
        with patch('subprocess.run', return_value=mock_result):
            result = await main.run_command("echo test")
            
            self.assertIn("Command output", result)
            self.assertIn("Stdout:", result)

    async def test_run_command_with_stderr(self):
        """Test run_command includes stderr in output."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = "Output"
        mock_result.stderr = "Error message"
        
        with patch('subprocess.run', return_value=mock_result):
            result = await main.run_command("test command")
            
            self.assertIn("Output", result)
            self.assertIn("Error message", result)
            self.assertIn("Stderr:", result)

    async def test_run_command_timeout(self):
        """Test run_command handles timeout."""
        with patch('subprocess.run', side_effect=subprocess.TimeoutExpired("cmd", 60)):
            result = await main.run_command("long_command")
            self.assertIn("timed out", result)

    async def test_run_command_error(self):
        """Test run_command handles general errors."""
        with patch('subprocess.run', side_effect=Exception("Command failed")):
            result = await main.run_command("bad_command")
            self.assertIn("Error executing command", result)

    async def test_search_files_success(self):
        """Test search_files finds matching files."""
        # Mock os.walk to simulate directory structure
        mock_walk_data = [
            ("/test", ["subdir"], ["file1.py", "file2.txt"]),
            ("/test/subdir", [], ["file3.py", "README.md"])
        ]
        
        with patch('os.walk', return_value=mock_walk_data):
            result = await main.search_files("*.py", "/test")
            
            self.assertIn("file1.py", result)
            self.assertIn("file3.py", result)
            self.assertNotIn("file2.txt", result)
            self.assertNotIn("README.md", result)

    async def test_read_file_line_range(self):
        """read_file returns only the requested line range."""
        content = "".join(f"line{i}\n" for i in range(10))
        with patch('builtins.open', mock_open(read_data=content)):
            result = await main.read_file("/test/path.txt", offset=2, limit=3)
        self.assertEqual(result, "line2\nline3\nline4\n")

    async def test_read_file_caps_large_file(self):
        """A file larger than the output bound is truncated with a range hint."""
        content = "x" * (main.MAX_OUTPUT_CHARS + 10000)
        with patch('builtins.open', mock_open(read_data=content)):
            result = await main.read_file("/test/big.txt")
        self.assertLess(len(result), len(content))
        self.assertIn("truncated: 10000 more chars", result)
        self.assertIn("offset=", result)

    async def test_read_file_reports_line_count_consistently(self):
        """The hint's line count must match what the range branch sees, or the
        model asks for an offset past the end."""
        content = "".join(f"{'x' * 200}\n" for _ in range(400))  # 400 lines, > cap
        with patch('builtins.open', mock_open(read_data=content)):
            result = await main.read_file("/test/big.txt")
        self.assertIn("has 400 lines", result)

    async def test_env_bounds_ignore_invalid_values(self):
        """A typo in MCP_MAX_OUTPUT_CHARS must not crash the server at import."""
        with patch.dict(os.environ, {"MCP_MAX_OUTPUT_CHARS": "lots"}):
            self.assertEqual(main._env_int("MCP_MAX_OUTPUT_CHARS", 50000), 50000)
        with patch.dict(os.environ, {"MCP_MAX_LIST_ENTRIES": "-3"}):
            self.assertEqual(main._env_int("MCP_MAX_LIST_ENTRIES", 500), 500)
        with patch.dict(os.environ, {"MCP_MAX_LIST_ENTRIES": "42"}):
            self.assertEqual(main._env_int("MCP_MAX_LIST_ENTRIES", 500), 42)

    async def test_list_files_caps_entries(self):
        """Huge directories are listed up to the entry bound."""
        items = [f"f{i:05d}" for i in range(main.MAX_LIST_ENTRIES + 7)]
        with patch('os.listdir', return_value=items):
            result = await main.list_files("/test/dir")
        self.assertIn("truncated: 7 more entries", result)
        self.assertNotIn(items[-1], result)

    async def test_run_command_keeps_stderr_when_stdout_is_huge(self):
        """A failure must stay visible even when stdout floods the output bound."""
        mock_result = MagicMock()
        mock_result.returncode = 2
        mock_result.stdout = "y" * (main.MAX_OUTPUT_CHARS + 10_000)
        mock_result.stderr = "fatal: something broke"
        with patch('subprocess.run', return_value=mock_result):
            result = await main.run_command("build")
        self.assertIn("fatal: something broke", result)
        self.assertIn("Exit code: 2", result)
        self.assertIn("truncated: 10000 more chars", result)

    async def test_run_command_caps_output(self):
        """Command output beyond the bound is truncated with a narrowing hint."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "y" * (main.MAX_OUTPUT_CHARS + 50)
        mock_result.stderr = ""
        with patch('subprocess.run', return_value=mock_result):
            result = await main.run_command("cat big")
        self.assertIn("truncated:", result)
        self.assertIn("head, tail or grep", result)

    async def test_search_files_error(self):
        """Test search_files handles errors gracefully."""
        with patch('os.walk', side_effect=PermissionError("Permission denied")):
            result = await main.search_files("*.py", "/test")
            self.assertIn("Error searching files", result)


def _ctx(session_key=None):
    """The smallest stand-in for FastMCP's Context: only the HTTP request headers."""
    headers = {} if session_key is None else {"x-dak-session-key": session_key}
    return SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(headers=headers)))


class TestSessionSandboxRouting(unittest.IsolatedAsyncioTestCase):
    """Tools run in the caller session's environment (docs/design/session-sandbox.md)."""

    async def test_off_mode_keeps_the_callers_path_even_with_a_session_header(self):
        # SANDBOX_MODE=off (the default): a ctx with a session key must not change path resolution.
        with patch.object(main, "_sandbox", SandboxManager(mode="off")), \
                patch('builtins.open', mock_open()) as mock_file, patch('os.makedirs'):
            await main.write_file("/test/path.txt", "x", ctx=_ctx("s1"))
        mock_file.assert_called_once_with("/test/path.txt", "w", encoding="utf-8")
        with patch.object(main, "_sandbox", SandboxManager(mode="off")), patch('subprocess.run') as run:
            await main.run_command("echo hi", ctx=_ctx("s1"))
        self.assertIsNone(run.call_args.kwargs["cwd"])

    async def test_inproc_rejects_paths_outside_the_session_workspace(self):
        with patch.object(main, "_sandbox", SandboxManager(mode="inproc")) as sandbox:
            try:
                for path in ("../escape.txt", "/etc/hostname"):
                    result = await main.read_file(path, ctx=_ctx("s1"))
                    self.assertIn("outside the session workspace", result)
                result = await main.write_file("../escape.txt", "x", ctx=_ctx("s1"))
                self.assertIn("outside the session workspace", result)
                result = await main.edit_file("../escape.txt", "a", "b", ctx=_ctx("s1"))
                self.assertIn("outside the session workspace", result)
                result = await main.search_files("*", "..", ctx=_ctx("s1"))
                self.assertIn("outside the session workspace", result)
                result = await main.grep("x", "/", ctx=_ctx("s1"))
                self.assertIn("outside the session workspace", result)
            finally:
                sandbox.destroy_all()

    async def test_inproc_run_command_runs_in_the_session_workdir(self):
        with patch.object(main, "_sandbox", SandboxManager(mode="inproc")) as sandbox:
            try:
                with patch('subprocess.run') as run:
                    await main.run_command("ls", ctx=_ctx("s1"))
                self.assertEqual(run.call_args.kwargs["cwd"], sandbox.ensure_session("s1")["workdir"])
            finally:
                sandbox.destroy_all()

    async def test_inproc_search_results_show_the_callers_paths(self):
        # The session directory behind the caller's path is not shown; a trailing / is not doubled.
        with patch.object(main, "_sandbox", SandboxManager(mode="inproc")) as sandbox:
            try:
                await main.write_file("sub/a.txt", "hello", ctx=_ctx("s1"))
                self.assertEqual(await main.search_files("*.txt", ".", ctx=_ctx("s1")), "./sub/a.txt")
                self.assertEqual(await main.search_files("*.txt", "sub/", ctx=_ctx("s1")), "sub/a.txt")
                self.assertEqual(await main.grep("hel", "./", ctx=_ctx("s1")), "./sub/a.txt:1: hello")
                self.assertEqual(await main.grep("hel", "sub/a.txt", ctx=_ctx("s1")), "sub/a.txt:1: hello")
                await main.edit_file("sub/a.txt", "hello", "bye", ctx=_ctx("s1"))
                self.assertEqual(await main.read_file("sub/a.txt", ctx=_ctx("s1")), "bye")
            finally:
                sandbox.destroy_all()

    async def test_missing_header_uses_the_default_session(self):
        with patch.object(main, "_sandbox", SandboxManager(mode="inproc")) as sandbox:
            try:
                await main.write_file("a.txt", "x", ctx=_ctx())
                await main.write_file("b.txt", "y")
                self.assertEqual(sorted(sandbox._sessions), ["default"])
                listing = await main.list_files(".", ctx=_ctx())
                self.assertEqual(listing.split(), ["a.txt", "b.txt"])
            finally:
                sandbox.destroy_all()

    async def test_write_file_uses_session_workdir_when_ctx_present(self):
        with patch.object(main, "_sandbox", SandboxManager(mode="inproc")) as sandbox:
            try:
                await main.write_file("sub/note.txt", "from s1", ctx=_ctx("s1"))
                workdir = sandbox.ensure_session("s1")["workdir"]
                with open(os.path.join(workdir, "sub", "note.txt")) as f:
                    self.assertEqual(f.read(), "from s1")
                # Another session sees none of it, through any file tool.
                self.assertIn("No such file", await main.read_file("sub/note.txt", ctx=_ctx("s2")))
                self.assertEqual(await main.list_files(".", ctx=_ctx("s2")), "")
                self.assertEqual(await main.search_files("*.txt", ".", ctx=_ctx("s2")), "")
                self.assertEqual(await main.grep("from", ".", ctx=_ctx("s2")), "No matches found.")
                # The owner finds it under the path it used, not the directory behind it.
                self.assertEqual(await main.search_files("*.txt", ".", ctx=_ctx("s1")), "./sub/note.txt")
                self.assertEqual(await main.grep("from", "sub", ctx=_ctx("s1")), "sub/note.txt:1: from s1")
            finally:
                sandbox.destroy_all()

    async def test_docker_run_command_executes_in_the_session_container(self):
        run = MagicMock()
        run.return_value = MagicMock(returncode=0, stdout="hi\n", stderr="")
        with patch.object(main, "_sandbox", SandboxManager(mode="docker", run=run)) as sandbox:
            result = await main.run_command("echo hi | cat", ctx=_ctx("s1"))
        cmd = run.call_args.args[0]
        name = sandbox._container_name("s1")
        self.assertEqual(cmd[:5], ["docker", "exec", "-w", "/workspace", name])
        self.assertEqual(cmd[-3:], ["sh", "-c", "echo hi | cat"])  # shell semantics kept
        self.assertIn("hi", result)

    async def test_docker_file_tools_refuse_instead_of_touching_the_server_files(self):
        run = MagicMock()
        with patch.object(main, "_sandbox", SandboxManager(mode="docker", run=run)), \
                patch('builtins.open', mock_open(read_data="server file")) as mock_file:
            result = await main.read_file("README.md", ctx=_ctx("s1"))
        self.assertIn("not available in SANDBOX_MODE=docker", result)
        mock_file.assert_not_called()
        run.assert_not_called()  # refusing must not start a container first


if __name__ == '__main__':
    unittest.main()
