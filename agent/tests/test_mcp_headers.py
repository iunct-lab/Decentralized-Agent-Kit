from types import SimpleNamespace

from google.adk.tools.mcp_tool import StreamableHTTPConnectionParams
from google.adk.tools.mcp_tool.mcp_session_manager import MCPSessionManager

from dak_agent.mcp_headers import SESSION_KEY_HEADER, session_key_header


def _ctx(user_id: str, session_id: str):
    return SimpleNamespace(user_id=user_id, session=SimpleNamespace(id=session_id))


def test_session_key_header_differs_for_different_sessions():
    assert session_key_header(_ctx("u1", "s1")) != session_key_header(_ctx("u2", "s2"))


def test_session_key_header_is_stable_for_same_session():
    assert session_key_header(_ctx("u1", "s1")) == session_key_header(_ctx("u1", "s1"))


def test_session_key_header_uses_expected_key():
    assert list(session_key_header(_ctx("u1", "s1"))) == [SESSION_KEY_HEADER]


def test_session_key_differentiates_mcp_session_pool():
    # ADK's real session manager: only the pool key is computed, nothing connects.
    manager = MCPSessionManager(StreamableHTTPConnectionParams(url="http://example.invalid/mcp"))
    key_a = manager._session_key_for(session_key_header(_ctx("u1", "s1")))
    key_b = manager._session_key_for(session_key_header(_ctx("u2", "s2")))
    assert key_a != key_b
    assert key_a == manager._session_key_for(session_key_header(_ctx("u1", "s1")))
