"""Central agent configuration: agent_config.yaml (MCP servers, A2A peers) and model selection."""
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

import yaml

logger = logging.getLogger(__name__)

_AGENT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# The ONLY place the fallback model is spelled out. Everything else (compose,
# docs, other modules) defers to this, so bumping the default is a one-line
# change here plus the matching sample value in .env.example
# (tests/test_config.py keeps the two in sync). Choosing a model at runtime
# never needs a code change: set MODEL_NAME (LiteLLM format).
DEFAULT_MODEL_NAME = "gemini-3.8-flash"

# Skills bundled with the agent; AGENT_SKILLS_DIRS replaces this list.
DEFAULT_SKILLS_DIR = os.path.join(_AGENT_ROOT, "skills")

# Candidate locations, in priority order: Docker image, repo checkout, CWD.
CONFIG_CANDIDATES = [
    "/app/agent_config.yaml",
    os.path.join(_AGENT_ROOT, "agent_config.yaml"),
    "agent_config.yaml",
]


@dataclass
class AgentConfig:
    """Parsed agent_config.yaml content."""
    mcp_servers: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    a2a_peers: List[Dict[str, Any]] = field(default_factory=list)
    # Tool permission rules (dak_agent.permission.load_rules), after the defaults.
    permission_rules: List[Dict[str, Any]] = field(default_factory=list)


def find_config_path(path: Optional[str] = None) -> Optional[str]:
    candidates = [path] if path else CONFIG_CANDIDATES
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate
    return None


def _resolve_mcp_server(srv: Dict[str, Any], env: Mapping[str, str]) -> Optional[Dict[str, Any]]:
    """Fill an `mcp_servers` entry from the environment, or None to drop it.

    `requires_env`: variables that must be set and non-blank, else the server
    (and every skill that names it) is off. `url_env`: a variable that, when
    set, replaces `url`. `bearer_token_env`: a variable sent as
    `Authorization: Bearer <value>`. Only variable names are logged.
    """
    missing = [name for name in srv.get("requires_env") or [] if not (env.get(name) or "").strip()]
    if missing:
        logger.info(f"MCP server '{srv['name']}' is disabled: {', '.join(missing)} not set")
        return None
    resolved = dict(srv)
    url = (env.get(srv["url_env"]) or "").strip() if srv.get("url_env") else ""
    if url:
        resolved["url"] = url
    token = (env.get(srv["bearer_token_env"]) or "").strip() if srv.get("bearer_token_env") else ""
    if token:
        resolved["headers"] = {"Authorization": f"Bearer {token}"}
    return resolved


def load_agent_config(path: Optional[str] = None, env: Optional[Mapping[str, str]] = None) -> AgentConfig:
    env = os.environ if env is None else env
    config_path = find_config_path(path)
    if not config_path:
        logger.warning("agent_config.yaml not found; using empty config.")
        return AgentConfig()

    try:
        with open(config_path, "r") as f:
            raw = yaml.safe_load(f) or {}
    except Exception as e:
        logger.warning(f"Failed to load agent config from {config_path}: {e}")
        return AgentConfig()

    mcp_servers = {}
    for srv in raw.get("mcp_servers") or []:
        if isinstance(srv, dict) and "name" in srv:
            resolved = _resolve_mcp_server(srv, env)
            if resolved is not None:
                mcp_servers[srv["name"]] = resolved
    a2a_peers = [peer for peer in raw.get("a2a_peers") or [] if isinstance(peer, dict)]
    permission_rules = list(raw.get("permissions") or [])

    logger.info(
        f"Loaded agent config from {config_path}: "
        f"{len(mcp_servers)} MCP server(s), {len(a2a_peers)} A2A peer(s)"
    )
    return AgentConfig(mcp_servers=mcp_servers, a2a_peers=a2a_peers, permission_rules=permission_rules)


def resolve_model_name(env: Optional[Mapping[str, str]] = None) -> str:
    """Pick the model: MODEL_NAME, else DEFAULT_MODEL_NAME.

    A blank MODEL_NAME counts as unset. It reaches the agent when .env carries
    a bare `MODEL_NAME=` line or the shell exports it empty (which also beats a
    non-empty .env value under compose); passing it on would hand LiteLLM an
    empty model name that only fails at the first request.
    """
    env = os.environ if env is None else env
    return (env.get("MODEL_NAME") or "").strip() or DEFAULT_MODEL_NAME


def get_litellm_model_name(model_name: str) -> str:
    """Prefix bare Gemini model names so LiteLLM uses Google AI Studio (API key) instead of Vertex AI."""
    if "gemini" in model_name and not model_name.startswith("gemini/"):
        return f"gemini/{model_name}"
    return model_name


def skills_dirs_from_env(env: Optional[Mapping[str, str]] = None) -> List[str]:
    """The skill directories: AGENT_SKILLS_DIRS (colon-separated), else DEFAULT_SKILLS_DIR.

    The agent loads its skills from these and the Agent Card lists the same ones.
    """
    env = os.environ if env is None else env
    dirs = [d.strip() for d in (env.get("AGENT_SKILLS_DIRS") or "").split(":") if d.strip()]
    return dirs or [DEFAULT_SKILLS_DIR]
