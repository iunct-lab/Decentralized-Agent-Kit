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
    permissions: List[Dict[str, Any]] = field(default_factory=list)


def find_config_path(path: Optional[str] = None) -> Optional[str]:
    candidates = [path] if path else CONFIG_CANDIDATES
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate
    return None


def load_agent_config(path: Optional[str] = None) -> AgentConfig:
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

    mcp_servers = {
        srv["name"]: srv for srv in raw.get("mcp_servers") or [] if isinstance(srv, dict) and "name" in srv
    }
    a2a_peers = [peer for peer in raw.get("a2a_peers") or [] if isinstance(peer, dict)]
    permissions = [rule for rule in raw.get("permissions") or [] if isinstance(rule, dict)]

    logger.info(
        f"Loaded agent config from {config_path}: "
        f"{len(mcp_servers)} MCP server(s), {len(a2a_peers)} A2A peer(s)"
    )
    return AgentConfig(mcp_servers=mcp_servers, a2a_peers=a2a_peers, permissions=permissions)


def resolve_model_name(env: Optional[Mapping[str, str]] = None) -> str:
    """Pick the model: MODEL_NAME, then legacy GEMINI_MODEL_NAME, then DEFAULT_MODEL_NAME.

    Blank values count as unset. An empty MODEL_NAME reaches the agent when
    .env carries a bare `MODEL_NAME=` line or the shell exports it empty (which
    also beats a non-empty .env value under compose); passing it on would hand
    LiteLLM an empty model name that only fails at the first request.

    GEMINI_MODEL_NAME is an undocumented legacy alias (its fate is tracked in
    PBI #72). Since compose stopped injecting a MODEL_NAME default it is
    reachable there too, so its use is logged rather than silent.
    """
    env = os.environ if env is None else env
    value = (env.get("MODEL_NAME") or "").strip()
    if value:
        return value
    legacy = (env.get("GEMINI_MODEL_NAME") or "").strip()
    if legacy:
        logger.warning(
            f"Model '{legacy}' comes from the legacy GEMINI_MODEL_NAME variable; "
            "set MODEL_NAME instead (it takes precedence)."
        )
        return legacy
    return DEFAULT_MODEL_NAME


def get_litellm_model_name(model_name: str) -> str:
    """Prefix bare Gemini model names so LiteLLM uses Google AI Studio (API key) instead of Vertex AI."""
    if "gemini" in model_name and not model_name.startswith("gemini/"):
        return f"gemini/{model_name}"
    return model_name
