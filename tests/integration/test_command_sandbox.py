"""run_command inside srt (MCP_COMMAND_SANDBOX=srt, docs/design/command-sandbox.md).

Runs only against the stack started with docker-compose.command-sandbox.yml on
top; the default stack skips the whole module.
"""
import uuid

import httpx
import pytest

from test_mcp_server import _initialize, _mcp_request, _parse_mcp_body


def _call(tool: str, **arguments) -> str:
    with httpx.Client() as client:
        headers = _initialize(client)
        resp = _mcp_request(client, headers, {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        })
    return _parse_mcp_body(resp)["result"]["content"][0]["text"]


def _run(command: str) -> str:
    return _call("run_command", command=command)


@pytest.fixture(scope="module", autouse=True)
def srt_enabled(stack_ready):
    out = _run("echo $MCP_COMMAND_SANDBOX")
    if out.startswith(("Exit code: 0\nStdout:\n\n", "Exit code: 0\nStdout:\noff\n")):  # unset or off
        pytest.skip("mcp-server is not running with MCP_COMMAND_SANDBOX=srt")
    # Enabled but srt cannot run here is a failure, not a skip.
    assert "Stdout:\nsrt" in out, out


def test_write_inside_projects_succeeds():
    name = f".srt-probe-{uuid.uuid4().hex}"
    out = _run(f"echo ok > /projects/{name} && cat /projects/{name} && rm /projects/{name}")
    assert "Exit code: 0" in out
    assert "ok" in out


def test_write_outside_allowed_fails():
    out = _run("touch /etc/dak-srt-probe")
    assert "Exit code: 0" not in out
    assert "Read-only file system" in out


def test_denied_read_fails():
    # srt masks a denied file with /dev/null: the .env compose reads (API keys),
    # mounted at /projects/.env, reads as an empty character device.
    out = _run("stat -c %F /projects/.env && wc -c < /projects/.env")
    assert "Exit code: 0" in out
    assert "character special file" in out
    assert "\n0\n" in out


def test_network_outside_allowlist_fails():
    out = _run(
        "python -c \"import urllib.request; "
        "urllib.request.urlopen('http://agent:8000/list-apps', timeout=5)\""
    )
    assert "Exit code: 0" not in out
    # Refused by srt's proxy (the only way out of the sandbox), not a DNS or connect error.
    assert "403" in out, out


def test_mcp_calls_still_work():
    # srt wraps only run_command: the server still answers and its file tools still read.
    assert "Decentralized Agent Kit" in _call("read_file", path="README.md")
