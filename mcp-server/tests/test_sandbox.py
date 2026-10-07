import importlib
import os
import shutil
import subprocess
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sandbox
from sandbox import SandboxManager


class TestSandboxManager(unittest.TestCase):
    """SandboxManager: which isolated environment a session's tool calls run in."""

    def test_default_mode_is_off_and_returns_no_workdir(self):
        # Regression guard: the default must keep the shared /projects behaviour.
        env = {k: v for k, v in os.environ.items() if not k.startswith("SANDBOX_")}
        with patch.dict(os.environ, env, clear=True):
            module = importlib.reload(sandbox)
            run = MagicMock()
            entry = module.SandboxManager(run=run).ensure_session("s1")
        importlib.reload(sandbox)
        self.assertEqual(entry["mode"], "off")
        self.assertIsNone(entry["workdir"])
        run.assert_not_called()

    def test_unknown_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            SandboxManager(mode="container")

    def test_ensure_session_creates_once_and_reuses(self):
        run = MagicMock()
        manager = SandboxManager(mode="docker", run=run)
        first = manager.ensure_session("s1")
        second = manager.ensure_session("s1")
        self.assertEqual(run.call_count, 2)  # rm of a leftover + run, once
        self.assertEqual(first["container_name"], second["container_name"])

        with patch("sandbox.tempfile.mkdtemp", return_value="/tmp/dak-sandbox-x") as mkdtemp:
            manager = SandboxManager(mode="inproc", run=MagicMock())
            manager.ensure_session("s1")
            manager.ensure_session("s1")
        self.assertEqual(mkdtemp.call_count, 1)

    def test_docker_mode_command_has_isolation_flags(self):
        run = MagicMock()
        entry = SandboxManager(mode="docker", run=run).ensure_session("s1")
        cmd = run.call_args.args[0]
        # A same-named leftover (a create that finished after our CLI timed out) is removed first.
        self.assertEqual(run.call_args_list[0].args[0], ["docker", "rm", "-f", entry["container_name"]])
        self.assertEqual(cmd[:3], ["docker", "run", "-d"])
        for flag in ("--network", "none", "--read-only", "--cap-drop", "ALL", "--user", "nobody",
                     f"--cpus={sandbox.SANDBOX_CPUS}", f"--memory={sandbox.SANDBOX_MEMORY}",
                     f"--pids-limit={sandbox.SANDBOX_PIDS_LIMIT}"):
            self.assertIn(flag, cmd)
        # /workspace must allow exec (Docker's tmpfs default is noexec) and /tmp stays writable.
        self.assertIn("/workspace:rw,exec,size=256m", cmd)
        self.assertIn("/tmp:rw,size=64m", cmd)
        self.assertIn("HOME=/workspace", cmd)
        self.assertIn("dak.sandbox=1", cmd)
        self.assertEqual(entry["workdir"], "/workspace")
        self.assertIn(entry["container_name"], cmd)

    def test_container_name_does_not_embed_the_session_key(self):
        name = SandboxManager(mode="docker", run=MagicMock())._container_name("alice:../../etc")
        self.assertRegex(name, r"^dak-sandbox-[0-9a-f]{16}$")

    def test_inproc_workdir_prefix_does_not_embed_the_session_key(self):
        # A header value is caller-controlled; it must not steer where the directory is made.
        with patch("sandbox.tempfile.mkdtemp", return_value="/tmp/x") as mkdtemp:
            SandboxManager(mode="inproc", run=MagicMock()).ensure_session("../../etc/x")
        prefix = mkdtemp.call_args.kwargs["prefix"]
        self.assertNotIn("/", prefix)
        self.assertNotIn("..", prefix)

    def test_destroy_session_removes_from_state(self):
        run = MagicMock()
        manager = SandboxManager(mode="docker", run=run)
        name = manager.ensure_session("s1")["container_name"]
        manager.destroy_session("s1")
        self.assertEqual(run.call_args.args[0], ["docker", "rm", "-f", name])
        manager.ensure_session("s1")
        self.assertEqual(run.call_count, 5)  # (rm, run), rm, (rm, run) again

        with patch("sandbox.tempfile.mkdtemp", return_value="/tmp/dak-sandbox-x") as mkdtemp, \
                patch("sandbox.shutil.rmtree") as rmtree:
            manager = SandboxManager(mode="inproc", run=MagicMock())
            manager.ensure_session("s1")
            manager.destroy_session("s1")
            rmtree.assert_called_once_with("/tmp/dak-sandbox-x", ignore_errors=True)
            manager.ensure_session("s1")
        self.assertEqual(mkdtemp.call_count, 2)

    def test_destroy_unknown_session_is_a_no_op(self):
        run = MagicMock()
        SandboxManager(mode="docker", run=run).destroy_session("never-made")
        run.assert_not_called()

    def test_sweep_removes_labelled_containers_left_by_a_killed_server(self):
        run = MagicMock()
        run.return_value.stdout = "abc123\ndef456\n"
        SandboxManager(mode="docker", run=run).sweep()
        self.assertEqual(run.call_args_list[0].args[0],
                         ["docker", "ps", "-aq", "--filter", "label=dak.sandbox=1"])
        self.assertEqual(run.call_args_list[1].args[0], ["docker", "rm", "-f", "abc123", "def456"])

    def test_sweep_does_nothing_outside_docker_mode_or_without_leftovers(self):
        run = MagicMock()
        SandboxManager(mode="inproc", run=run).sweep()
        run.assert_not_called()
        run.return_value.stdout = ""
        SandboxManager(mode="docker", run=run).sweep()
        self.assertEqual(run.call_count, 1)  # ps only, no rm

    def test_reap_expired_destroys_only_old_sessions(self):
        run = MagicMock()
        manager = SandboxManager(mode="docker", ttl_seconds=10, run=run)
        manager.ensure_session("old")
        manager.ensure_session("new")
        now = manager._sessions["new"]["last_used"]
        manager._sessions["old"]["last_used"] = now - 11
        self.assertEqual(manager.reap_expired(now=now), ["old"])
        self.assertEqual(sorted(manager._sessions), ["new"])

    def test_docker_calls_have_a_timeout(self):
        run = MagicMock()
        run.return_value.stdout = "abc123\n"
        manager = SandboxManager(mode="docker", run=run)
        manager.ensure_session("s1")
        manager.destroy_session("s1")
        manager.sweep()
        for call in run.call_args_list:
            self.assertIn("timeout", call.kwargs)

    def test_a_timed_out_removal_does_not_stop_the_other_removals(self):
        run = MagicMock()
        manager = SandboxManager(mode="docker", run=run)
        manager.ensure_session("a")
        manager.ensure_session("b")
        run.reset_mock()
        run.side_effect = subprocess.TimeoutExpired("docker rm", 60)
        with patch("builtins.print") as warn:
            manager.destroy_all()  # sweep() at the next start removes what timed out
        self.assertEqual(manager._sessions, {})
        self.assertEqual(run.call_count, 2)
        self.assertIn("timed out", warn.call_args.args[0])  # the operator gets a signal

    def test_exec_in_session_runs_inside_the_container_with_a_time_limit(self):
        run = MagicMock()
        manager = SandboxManager(mode="docker", run=run)
        name = manager.ensure_session("s1")["container_name"]
        manager.exec_in_session("s1", ["sh", "-c", "ls"])
        cmd = run.call_args.args[0]
        # timeout inside the container: killing the docker CLI alone leaves the process running.
        self.assertEqual(cmd, ["docker", "exec", "-w", "/workspace", name, "timeout", "60", "sh", "-c", "ls"])
        self.assertIn("timeout", run.call_args.kwargs)

    def test_exec_in_session_inproc_uses_the_workdir(self):
        run = MagicMock()
        with patch("sandbox.tempfile.mkdtemp", return_value="/tmp/dak-sandbox-x"):
            manager = SandboxManager(mode="inproc", run=run)
            manager.exec_in_session("s1", ["ls"])
        self.assertEqual(run.call_args.args[0], ["ls"])
        self.assertEqual(run.call_args.kwargs["cwd"], "/tmp/dak-sandbox-x")

    def test_two_session_keys_get_different_inproc_workdirs(self):
        # Real mkdtemp/rmtree: no Docker needed.
        manager = SandboxManager(mode="inproc")
        try:
            first = manager.ensure_session("alice:s1")["workdir"]
            second = manager.ensure_session("bob:s2")["workdir"]
            self.assertNotEqual(first, second)
            self.assertTrue(os.path.isdir(first) and os.path.isdir(second))
        finally:
            manager.destroy_all()

    def test_inproc_workdir_is_removed_on_destroy(self):
        manager = SandboxManager(mode="inproc")
        workdir = manager.ensure_session("s1")["workdir"]
        try:
            with open(os.path.join(workdir, "left.txt"), "w") as f:
                f.write("x")
            manager.destroy_session("s1")
            self.assertFalse(os.path.exists(workdir))
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def test_inproc_workdir_is_removed_after_the_ttl(self):
        manager = SandboxManager(mode="inproc", ttl_seconds=10)
        workdir = manager.ensure_session("s1")["workdir"]
        try:
            self.assertEqual(manager.reap_expired(now=time.monotonic() + 11), ["s1"])
            self.assertFalse(os.path.exists(workdir))
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def test_destroy_all_removes_every_session(self):
        run = MagicMock()
        manager = SandboxManager(mode="docker", run=run)
        manager.ensure_session("a")
        manager.ensure_session("b")
        manager.destroy_all()
        self.assertEqual(manager._sessions, {})
        removed = {call.args[0][3] for call in run.call_args_list[-2:] if call.args[0][:3] == ["docker", "rm", "-f"]}
        self.assertEqual(removed, {manager._container_name("a"), manager._container_name("b")})


class TestSocketExposure(unittest.TestCase):
    """A container that sees the Docker socket must not run tools outside SANDBOX_MODE=docker."""

    def test_socket_in_container_is_refused_unless_docker_mode(self):
        for mode in ("off", "inproc"):
            with self.subTest(mode=mode), self.assertRaises(RuntimeError):
                sandbox.check_socket_exposure(mode, in_container=True, socket_visible=True)

    def test_socket_in_container_is_allowed_in_docker_mode(self):
        sandbox.check_socket_exposure("docker", in_container=True, socket_visible=True)

    def test_socket_on_the_host_is_allowed(self):
        # Run directly on a developer's machine (uv run main.py): the socket is
        # usually there, and the default mode must keep working.
        sandbox.check_socket_exposure("off", in_container=False, socket_visible=True)

    def test_no_socket_in_container_is_allowed(self):
        sandbox.check_socket_exposure("off", in_container=True, socket_visible=False)


if __name__ == "__main__":
    unittest.main()
