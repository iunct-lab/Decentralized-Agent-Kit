import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import command_sandbox
import main
from sandbox import SandboxManager

SETTINGS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "srt-settings.json")


def _ok():
    return MagicMock(returncode=0, stdout="hi\n", stderr="")


def _env(**values):
    """os.environ without MCP_COMMAND_SANDBOX / MCP_SRT_SETTINGS, plus `values`."""
    env = {k: v for k, v in os.environ.items()
           if k not in (command_sandbox.COMMAND_SANDBOX_ENV, command_sandbox.SETTINGS_ENV)}
    env.update(values)
    return patch.dict(os.environ, env, clear=True)


class TestCommandSandbox(unittest.IsolatedAsyncioTestCase):
    """run_command inside srt (MCP_COMMAND_SANDBOX=srt, docs/design/command-sandbox.md)."""

    async def test_default_mode_runs_shell_as_before(self):
        with _env(), patch.object(main, "_sandbox", SandboxManager(mode="off")), \
                patch("main.subprocess.run", return_value=_ok()) as run:
            result = await main.run_command("echo hi")
        self.assertEqual(run.call_args.args[0], "echo hi")
        self.assertTrue(run.call_args.kwargs["shell"])
        self.assertIsNone(run.call_args.kwargs["cwd"])
        self.assertIn("hi", result)

    async def test_srt_mode_wraps_command(self):
        with _env(MCP_COMMAND_SANDBOX="srt", MCP_SRT_SETTINGS=SETTINGS), \
                patch.object(main, "_sandbox", SandboxManager(mode="off")), \
                patch("command_sandbox.shutil.which", return_value="/usr/local/bin/srt"), \
                patch("main.subprocess.run", return_value=_ok()) as run:
            result = await main.run_command("echo hi")
        self.assertEqual(run.call_args.args[0], ["srt", "--settings", SETTINGS, "echo hi"])
        self.assertFalse(run.call_args.kwargs["shell"])
        # /app (the server's code) is read-only inside srt; commands run in the workspace.
        self.assertEqual(run.call_args.kwargs["cwd"], "/projects")
        self.assertIn("hi", result)

    async def test_srt_mode_keeps_the_inproc_session_workdir(self):
        with _env(MCP_COMMAND_SANDBOX="srt", MCP_SRT_SETTINGS=SETTINGS), \
                patch.object(main, "_sandbox", SandboxManager(mode="inproc")) as sandbox, \
                patch("command_sandbox.shutil.which", return_value="/usr/local/bin/srt"), \
                patch("main.subprocess.run", return_value=_ok()) as run:
            try:
                await main.run_command("ls", ctx=None)
                workdir = sandbox.ensure_session("default")["workdir"]
            finally:
                sandbox.destroy_all()
        self.assertEqual(run.call_args.kwargs["cwd"], workdir)

    async def test_srt_missing_does_not_run(self):
        with _env(MCP_COMMAND_SANDBOX="srt", MCP_SRT_SETTINGS=SETTINGS), \
                patch.object(main, "_sandbox", SandboxManager(mode="off")), \
                patch("command_sandbox.shutil.which", return_value=None), \
                patch("main.subprocess.run") as run:
            result = await main.run_command("echo hi")
        run.assert_not_called()
        self.assertIn("was not run", result)
        self.assertIn("srt", result)

    async def test_settings_missing_does_not_run(self):
        with _env(MCP_COMMAND_SANDBOX="srt", MCP_SRT_SETTINGS="/nonexistent/srt.json"), \
                patch.object(main, "_sandbox", SandboxManager(mode="off")), \
                patch("command_sandbox.shutil.which", return_value="/usr/local/bin/srt"), \
                patch("main.subprocess.run") as run:
            result = await main.run_command("echo hi")
        run.assert_not_called()
        self.assertIn("was not run", result)

    async def test_unknown_mode_does_not_run(self):
        # A typo must not silently drop the sandbox.
        with _env(MCP_COMMAND_SANDBOX="srtt"), patch.object(main, "_sandbox", SandboxManager(mode="off")), \
                patch("main.subprocess.run") as run:
            result = await main.run_command("echo hi")
        run.assert_not_called()
        self.assertIn("was not run", result)
        self.assertIn("srtt", result)

    async def test_srt_with_docker_sessions_does_not_run(self):
        # The command would run in the session container, outside srt.
        docker = MagicMock()
        with _env(MCP_COMMAND_SANDBOX="srt", MCP_SRT_SETTINGS=SETTINGS), \
                patch.object(main, "_sandbox", SandboxManager(mode="docker", run=docker)), \
                patch("command_sandbox.shutil.which", return_value="/usr/local/bin/srt"), \
                patch("main.subprocess.run") as run:
            result = await main.run_command("echo hi")
        run.assert_not_called()
        docker.assert_not_called()  # no session container is started either
        self.assertIn("was not run", result)


class TestDefaultSettings(unittest.TestCase):
    def test_writes_only_to_the_workspace_and_tmp_and_denies_the_network(self):
        import json
        with open(SETTINGS, encoding="utf-8") as f:
            settings = json.load(f)
        self.assertEqual(settings["filesystem"]["allowWrite"], ["/projects", "/tmp"])
        self.assertEqual(settings["network"]["allowedDomains"], [])
        self.assertFalse(settings["network"]["allowLocalBinding"])
        self.assertNotIn("enableWeakerNestedSandbox", settings)


if __name__ == "__main__":
    unittest.main()
