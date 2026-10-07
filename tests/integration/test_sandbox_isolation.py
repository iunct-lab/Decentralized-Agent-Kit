"""SANDBOX_MODE=docker on a real Docker daemon (PBI #20, docs/design/session-sandbox.md).

Runs only with DAK_SANDBOX_DOCKER_TESTS=1, against the stack started with the
opt-in socket override (and a short TTL for the reaper test):

    SANDBOX_TTL_SECONDS=5 docker compose -f docker-compose.yml -f docker-compose.test.yml \
        -f docker-compose.sandbox.yml up -d --build --wait
    DAK_SANDBOX_DOCKER_TESTS=1 uv run pytest test_sandbox_isolation.py -q

The test process needs the same daemon (it runs `docker inspect` / `docker ps`).
"""
import hashlib
import json
import os
import subprocess
import time
import uuid

import httpx
import pytest

from test_mcp_server import _initialize, _mcp_request, _parse_mcp_body

pytestmark = pytest.mark.skipif(
    os.getenv("DAK_SANDBOX_DOCKER_TESTS") != "1",
    reason="Run only on a stack where mounting the Docker socket is approved (#20): set DAK_SANDBOX_DOCKER_TESTS=1",
)

TTL_SECONDS = int(os.getenv("SANDBOX_TTL_SECONDS", "900"))


def _call(session_key: str, tool: str, args: dict) -> str:
    """Call one tool as the agent would for this session (X-DAK-Session-Key, #19)."""
    with httpx.Client() as client:
        headers = _initialize(client)
        headers["X-DAK-Session-Key"] = session_key
        resp = _mcp_request(client, headers, {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool, "arguments": args},
        })
    return _parse_mcp_body(resp)["result"]["content"][0]["text"]


def _key() -> str:
    return f"it-user:{uuid.uuid4()}"


def _container(session_key: str) -> str:
    return "dak-sandbox-" + hashlib.sha256(session_key.encode()).hexdigest()[:16]


def _sandbox_containers() -> set[str]:
    out = subprocess.run(["docker", "ps", "-a", "--filter", "label=dak.sandbox=1", "--format", "{{.Names}}"],
                         check=True, capture_output=True, text=True).stdout
    return set(out.split())


def test_two_sessions_cannot_see_each_others_files():
    a, b = _key(), _key()
    _call(a, "run_command", {"command": "echo a > a.txt"})
    _call(a, "write_file", {"path": "notes/b.txt", "content": "from a"})
    assert "a.txt" in _call(a, "run_command", {"command": "ls"})
    assert _call(a, "read_file", {"path": "notes/b.txt"}) == "from a"

    listing = _call(b, "run_command", {"command": "ls -A"})
    assert "a.txt" not in listing and "notes" not in listing
    assert _call(b, "list_files", {"path": "."}) == ""
    assert "No such file" in _call(b, "read_file", {"path": "notes/b.txt"})
    assert _call(b, "grep", {"pattern": "from", "path": "."}) == "No matches found."


def test_network_is_blocked_in_sandbox():
    probe = ("python3 -c \"import urllib.request; urllib.request.urlopen('https://example.com', timeout=5)\""
             " && echo REACHED || echo BLOCKED")
    out = _call(_key(), "run_command", {"command": probe})
    assert "BLOCKED" in out and "REACHED" not in out


def test_resource_limits_are_applied():
    key = _key()
    # What the kernel enforces in the container's cgroup: CPU quota (per 100 ms), memory and PID limits.
    # cgroup v2 has cpu.max / memory.max / pids.max at the root; v1 has one directory per controller.
    read_cgroup = ("if [ -f /sys/fs/cgroup/cpu.max ]; then cut -d' ' -f1 /sys/fs/cgroup/cpu.max;"
                   " cat /sys/fs/cgroup/memory.max /sys/fs/cgroup/pids.max;"
                   " else cat /sys/fs/cgroup/cpu/cpu.cfs_quota_us /sys/fs/cgroup/memory/memory.limit_in_bytes"
                   " /sys/fs/cgroup/pids/pids.max; fi")
    cgroup = _call(key, "run_command", {"command": read_cgroup})
    print(f"cgroup inside the sandbox: {cgroup}")
    inspected = subprocess.run(["docker", "inspect", _container(key)], check=True, capture_output=True, text=True)
    host_config = json.loads(inspected.stdout)[0]["HostConfig"]
    cpus = float(os.getenv("SANDBOX_CPUS", "1"))
    memory = os.getenv("SANDBOX_MEMORY", "512m")
    assert memory.endswith("m")  # the only unit this test converts
    pids = int(os.getenv("SANDBOX_PIDS_LIMIT", "128"))

    assert host_config["NanoCpus"] == int(cpus * 1e9)
    assert host_config["Memory"] == int(memory[:-1]) * 1024 * 1024
    assert host_config["PidsLimit"] == pids
    assert host_config["NetworkMode"] == "none"
    assert host_config["ReadonlyRootfs"] is True
    assert host_config["CapDrop"] == ["ALL"]
    # Enforced, not only configured: the values reach the container's cgroup.
    quota, limit, pids_max = cgroup.split("Stdout:\n", 1)[1].split()[:3]
    assert (quota, limit, pids_max) == (str(int(cpus * 100000)), str(host_config["Memory"]), str(pids)), cgroup


@pytest.mark.skipif(TTL_SECONDS > 10, reason="needs the stack started with SANDBOX_TTL_SECONDS<=10")
def test_session_destroyed_after_ttl():
    key = _key()
    _call(key, "run_command", {"command": "true"})
    assert _container(key) in _sandbox_containers()
    deadline = time.time() + 70
    while time.time() < deadline and _container(key) in _sandbox_containers():
        time.sleep(2)
    assert _container(key) not in _sandbox_containers()
