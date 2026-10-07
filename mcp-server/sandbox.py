"""Per-session disposable isolation for the MCP tools (docs/design/session-sandbox.md).

SandboxManager decides *where* an already-allowed tool call runs, never
*whether* it may run (that is the agent's before_tool_callback, #16).

SANDBOX_MODE:
  off     (default) no isolation; tools keep using the shared /projects.
  inproc  one temporary directory per session inside this process. Separates
          the file tools only; run_command can still reach anything.
  docker  one disposable container per session (no network, read-only root,
          tmpfs workspace, CPU/memory/PID limits, no capabilities, non-root),
          driven through the docker CLI.
"""
import hashlib
import os
import shutil
import subprocess
import tempfile
import threading
import time

MODES = ("off", "inproc", "docker")

SANDBOX_MODE = os.getenv("SANDBOX_MODE", "off")
SANDBOX_IMAGE = os.getenv("SANDBOX_IMAGE", "python:3.12-slim")
SANDBOX_TTL_SECONDS = int(os.getenv("SANDBOX_TTL_SECONDS", "900"))
SANDBOX_CPUS = os.getenv("SANDBOX_CPUS", "1")
SANDBOX_MEMORY = os.getenv("SANDBOX_MEMORY", "512m")
SANDBOX_PIDS_LIMIT = os.getenv("SANDBOX_PIDS_LIMIT", "128")

DOCKER_WORKDIR = "/workspace"
# Marks our containers so a restarted server can remove the ones a killed server left behind.
DOCKER_LABEL = "dak.sandbox=1"
DOCKER_SOCKET = "/var/run/docker.sock"


def check_socket_exposure(mode: str, in_container: bool, socket_visible: bool) -> None:
    """Refuse a container that sees the Docker socket outside SANDBOX_MODE=docker.

    There, run_command runs unisolated next to a socket that drives the host's
    daemon. Run directly on a host, the server already has the developer's own
    rights, so a socket there adds nothing and is allowed.
    """
    if in_container and socket_visible and mode != "docker":
        raise RuntimeError(
            f"{DOCKER_SOCKET} is mounted but SANDBOX_MODE={mode}: run_command would reach the host's "
            "Docker daemon unisolated. Use docker-compose.sandbox.yml (SANDBOX_MODE=docker) or drop the mount."
        )


class SandboxManager:
    def __init__(self, mode=SANDBOX_MODE, ttl_seconds=SANDBOX_TTL_SECONDS, run=subprocess.run):
        # An unknown mode must stop the server: silently falling back to "off"
        # would look isolated while it is not.
        if mode not in MODES:
            raise ValueError(f"SANDBOX_MODE must be one of {', '.join(MODES)}; got {mode!r}")
        self.mode = mode
        self._ttl = ttl_seconds
        self._run = run
        self._sessions: dict[str, dict] = {}
        self._lock = threading.Lock()

    def _container_name(self, session_key: str) -> str:
        # Hash the key: it is a caller-supplied header and must not reach a name or a path as is.
        return "dak-sandbox-" + hashlib.sha256(session_key.encode()).hexdigest()[:16]

    def _create(self, session_key: str) -> dict:
        if self.mode == "off":
            return {"mode": "off", "container_name": None, "workdir": None}
        name = self._container_name(session_key)
        if self.mode == "docker":
            cmd = [
                "docker", "run", "-d", "--name", name, "--label", DOCKER_LABEL,
                "--network", "none",
                # Docker's tmpfs is noexec by default: scripts in the workspace must run.
                "--read-only", "--tmpfs", f"{DOCKER_WORKDIR}:rw,exec,size=256m", "--tmpfs", "/tmp:rw,size=64m",
                "--env", f"HOME={DOCKER_WORKDIR}",
                f"--cpus={SANDBOX_CPUS}", f"--memory={SANDBOX_MEMORY}", f"--pids-limit={SANDBOX_PIDS_LIMIT}",
                "--cap-drop", "ALL", "--user", "nobody",
                SANDBOX_IMAGE, "sleep", "infinity",
            ]
            # A create that finished after an earlier `docker run` timed out leaves this name behind.
            self._remove_container(name)
            # Bounded: a hung daemon must not hold the lock forever (300 s leaves room for an image pull).
            self._run(cmd, check=True, capture_output=True, text=True, timeout=300)
            return {"mode": "docker", "container_name": name, "workdir": DOCKER_WORKDIR}
        workdir = tempfile.mkdtemp(prefix=f"{name}-")
        return {"mode": "inproc", "container_name": None, "workdir": workdir}

    def ensure_session(self, session_key: str) -> dict:
        """The session's environment, created on first use."""
        with self._lock:
            entry = self._sessions.get(session_key)
            if entry is None:
                entry = self._create(session_key)
                self._sessions[session_key] = entry
            entry["last_used"] = time.monotonic()
            return entry

    def _dispose(self, session_key: str) -> None:
        # Caller holds the lock, so a concurrent ensure_session cannot reuse the
        # entry or recreate the container name before the removal has finished.
        entry = self._sessions.pop(session_key, None)
        if entry is None:
            return
        if entry["mode"] == "docker":
            self._remove_container(entry["container_name"])
        elif entry["mode"] == "inproc":
            shutil.rmtree(entry["workdir"], ignore_errors=True)

    def _remove_container(self, *names: str) -> None:
        # Best effort: a timed-out removal must not stop the others; sweep() at the next start catches it.
        try:
            self._run(["docker", "rm", "-f", *names], check=False, capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            print(f"Warning: docker rm -f {' '.join(names)} timed out; left for the next sweep()")

    def destroy_session(self, session_key: str) -> None:
        with self._lock:
            self._dispose(session_key)

    def exec_in_session(self, session_key: str, command: list[str], input: str | None = None,
                        text: bool = True) -> subprocess.CompletedProcess:
        """Run an argv in the session's environment (docker: inside its container).

        `input` goes to the command's stdin; `text=False` returns stdout as bytes (a tar stream).
        """
        entry = self.ensure_session(session_key)
        if entry["mode"] == "docker":
            # `timeout` inside the container: killing the docker CLI alone leaves the process running.
            stdin = ["-i"] if input is not None else []
            command = ["docker", "exec", *stdin, "-w", DOCKER_WORKDIR, entry["container_name"],
                       "timeout", "60", *command]
        return self._run(command, cwd=entry["workdir"] if entry["mode"] == "inproc" else None,
                         input=input, capture_output=True, text=text, timeout=65)

    def reap_expired(self, now: float | None = None) -> list[str]:
        """Destroy the sessions idle for longer than the TTL; return their keys."""
        now = time.monotonic() if now is None else now
        with self._lock:
            expired = [key for key, entry in self._sessions.items() if now - entry["last_used"] > self._ttl]
            for key in expired:
                self._dispose(key)
        return expired

    def sweep(self) -> None:
        """Remove labelled containers a killed server left behind (call at startup)."""
        if self.mode != "docker":
            return
        listed = self._run(["docker", "ps", "-aq", "--filter", f"label={DOCKER_LABEL}"],
                           check=True, capture_output=True, text=True, timeout=60)
        ids = listed.stdout.split()
        if ids:
            self._remove_container(*ids)

    def destroy_all(self) -> None:
        """Destroy every session (server shutdown): no container outlives the server."""
        with self._lock:
            for key in list(self._sessions):
                self._dispose(key)
