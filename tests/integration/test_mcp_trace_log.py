"""The agent's tool_call log line and the mcp-server's carry the same trace ID (#630).

The mcp SDK puts the current trace context in `_meta.traceparent` of every request, so
`docker compose logs agent mcp-server | grep <trace_id>` follows one tool call across both."""
import re
import subprocess
import uuid
from pathlib import Path

from conftest import function_responses

MODEL = "fake-default"
ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ["docker", "compose", "-f", "docker-compose.yml", "-f", "docker-compose.test.yml"]
AGENT_LINE = re.compile(r"tool_call name=read_file trace_id=(\S+)")
MCP_LINE = re.compile(r"tool_call name=read_file session=\S+ trace_id=(\S+) ok=true")


def _trace_ids(service: str, pattern: re.Pattern) -> list:
    logs = subprocess.run([*COMPOSE, "logs", "--no-log-prefix", service], cwd=ROOT, capture_output=True,
                          text=True, check=True)
    return pattern.findall(logs.stdout + logs.stderr)


def test_agent_and_mcp_server_share_trace_id(agent, fake_llm):
    # the logs keep earlier calls: only lines added by this call count
    agent_before = len(_trace_ids("agent", AGENT_LINE))
    mcp_before = len(_trace_ids("mcp-server", MCP_LINE))
    fake_llm.clear(MODEL)
    fake_llm.script(MODEL, [
        fake_llm.tool_call("enable_skill", skill_name="filesystem"),
        fake_llm.tool_call("read_file", path=f"trace-{uuid.uuid4().hex}.txt"),
        fake_llm.text("done"),
    ])
    events = agent.run(agent.create_session(), "Enable the filesystem skill and read a file")
    assert any(r.get("name") == "read_file" for r in function_responses(events))

    agent_ids = _trace_ids("agent", AGENT_LINE)
    mcp_ids = _trace_ids("mcp-server", MCP_LINE)
    assert len(agent_ids) == agent_before + 1, "the agent did not log this call"
    assert len(mcp_ids) == mcp_before + 1, "the mcp-server did not log this call"
    assert re.fullmatch(r"[0-9a-f]{32}", agent_ids[-1]), agent_ids[-1]
    assert mcp_ids[-1] == agent_ids[-1]
