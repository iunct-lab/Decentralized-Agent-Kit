"""
A2A Peer Manager: loads A2A peers from agent_config.yaml and exposes them
as remote sub-agents. Config-based, like MCP servers.
"""
import logging
import os
from typing import List, Optional
from urllib.parse import urlparse

from .config import load_agent_config

logger = logging.getLogger(__name__)

try:
    from google.adk.agents.remote_a2a_agent import RemoteA2aAgent
    A2A_AVAILABLE = True
except ImportError:
    A2A_AVAILABLE = False
    logger.warning("RemoteA2aAgent not available. A2A peer functionality disabled.")


class A2APeerConfig:
    """Configuration for a single A2A peer."""

    def __init__(self, name: str, url: str, capabilities: List[str] = None, card_url: Optional[str] = None):
        self.name = name
        self.url = url
        self.capabilities = capabilities or []
        self.card_url = card_url

    def __repr__(self):
        return f"A2APeer({self.name}, {self.url}, card_url={self.card_url}, caps={self.capabilities})"


def load_a2a_peers_from_config(config_path: str = None) -> List[A2APeerConfig]:
    """Load A2A peer configurations from agent_config.yaml."""
    config = load_agent_config(config_path)
    peers = []
    for peer_config in config.a2a_peers:
        peer = A2APeerConfig(
            name=peer_config.get("name"),
            url=peer_config.get("url"),
            capabilities=peer_config.get("capabilities", []),
            card_url=peer_config.get("card_url"),
        )
        peers.append(peer)
        logger.info(f"Loaded A2A peer: {peer}")
    return peers


CARD_PATH = "/.well-known/agent-card.json"


def resolve_agent_card_url(peer: A2APeerConfig) -> str:
    """Where the peer's Agent Card is. `card_url` if set; else `url` is the
    card itself or the peer's A2A endpoint (`http://host:8000/a2a/<app>`),
    which serves the card under it. A bare host (the old form) still gets
    DAK's default endpoint `/a2a/dak_agent`, with a warning."""
    if peer.card_url:
        return peer.card_url
    url = peer.url.rstrip("/")
    if url.endswith(CARD_PATH):
        return url
    if urlparse(url).path in ("", "/"):
        logger.warning(
            f"A2A peer {peer.name}: url {peer.url} has no path; assuming DAK's endpoint /a2a/dak_agent. "
            f"Write the peer's A2A endpoint instead (e.g. {url}/a2a/dak_agent), or set card_url.")
        url += "/a2a/dak_agent"
    return url + CARD_PATH


def create_remote_a2a_agents(peers: List[A2APeerConfig]) -> List["RemoteA2aAgent"]:
    """Create RemoteA2aAgent instances from peer configurations."""
    if not A2A_AVAILABLE:
        logger.warning("RemoteA2aAgent not available. Returning empty list.")
        return []

    agents = []
    for peer in peers:
        try:
            caps_str = ", ".join(peer.capabilities) if peer.capabilities else "general purpose"
            description = f"Remote agent '{peer.name}' at {peer.url}. Capabilities: {caps_str}"

            agent_card_url = resolve_agent_card_url(peer)

            agent = RemoteA2aAgent(
                name=peer.name,
                agent_card=agent_card_url,
                description=description,
            )
            agents.append(agent)
            logger.info(f"Created RemoteA2aAgent: {peer.name} -> {agent_card_url}")
        except Exception as e:
            logger.error(f"Failed to create RemoteA2aAgent for {peer.name}: {e}")

    return agents


def get_a2a_sub_agents() -> List["RemoteA2aAgent"]:
    """
    Load A2A peers from config and create remote sub-agents.

    Set ENABLE_A2A_CONSUMER=true to enable (Consumer mode). Provider agents
    should not set this, to avoid delegation loops.
    """
    if os.getenv("ENABLE_A2A_CONSUMER", "false").lower() != "true":
        logger.info("A2A Consumer mode disabled. No sub-agents loaded.")
        return []

    peers = load_a2a_peers_from_config()
    return create_remote_a2a_agents(peers)
