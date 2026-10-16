"""
A2A Peer Manager: loads A2A peers from agent_config.yaml and exposes them
as remote sub-agents. Config-based, like MCP servers.
"""
import logging
import os
from typing import List, Optional
from urllib.parse import urlparse

import httpx
from a2a.client.card_resolver import parse_agent_card
from a2a.types import AgentCard
from a2a.utils.signing import NoSignatureError, create_signature_verifier

from .config import load_agent_config

logger = logging.getLogger(__name__)

try:
    from google.adk.a2a.agent.config import A2aRemoteAgentConfig
    from google.adk.agents.remote_a2a_agent import RemoteA2aAgent
    A2A_AVAILABLE = True
except ImportError:
    A2A_AVAILABLE = False
    logger.warning("RemoteA2aAgent not available. A2A peer functionality disabled.")


class A2APeerConfig:
    """Configuration for a single A2A peer."""

    def __init__(self, name: str, url: str, capabilities: List[str] = None, card_url: Optional[str] = None,
                 card_public_key_file: Optional[str] = None):
        self.name = name
        self.url = url
        self.capabilities = capabilities or []
        self.card_url = card_url
        self.card_public_key_file = card_public_key_file

    def __repr__(self):
        return (f"A2APeer({self.name}, {self.url}, card_url={self.card_url}, "
                f"card_public_key_file={self.card_public_key_file}, caps={self.capabilities})")


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
            card_public_key_file=peer_config.get("card_public_key_file"),
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


def fetch_agent_card(card_url: str, timeout: float = 10.0) -> AgentCard:
    """The peer's card, fetched separately from the one ADK fetches on the
    first delegation, so the signature can be checked before the agent decides."""
    resp = httpx.get(card_url, timeout=timeout)
    resp.raise_for_status()
    return parse_agent_card(resp.json())


def check_card_signature(card: Optional[AgentCard], public_key_file: Optional[str],
                         fetch_error: Optional[str]) -> str:
    """One line for the sub-agent's description and the log:
    verified (kid) / unsigned / invalid (error) / unchecked (reason).

    Any failure other than a missing signature is `invalid`, not only
    InvalidSignaturesError: a broken signature fails while being decoded.
    A public key file that cannot be read is a configuration mistake and raises.
    """
    if not public_key_file:
        return "card signature: unchecked (no card_public_key_file)"
    with open(public_key_file, "rb") as f:
        public_pem = f.read()
    if card is None:
        return f"card signature: unchecked (card not fetched: {fetch_error})"
    tried = []

    def key_provider(kid, jku):  # one key per peer; jku is not followed
        tried.append(kid)
        return public_pem

    try:
        create_signature_verifier(key_provider, ["ES256"])(card)
    except NoSignatureError:
        return "card signature: unsigned"
    except Exception as e:
        return f"card signature: invalid ({type(e).__name__})"
    # The verifier stops at the first signature that passes
    return f"card signature: verified (kid={tried[-1]})"


# The card's text is the peer's to write: cut it where ADK cuts a card's description
# (google/adk/a2a/agent/_remote_a2a_agent.py, _MAX_CARD_DESCRIPTION_CHARS)
MAX_CARD_TEXT_CHARS = 1024
CARD_TEXT_TRUNCATED = " …(card text truncated)"


def card_text(card: Optional[AgentCard]) -> str:
    """What the peer's card says it does: its description and skills, for the
    sub-agent's description. Empty without a card."""
    if card is None:
        return ""
    text = f"Card: {card.description}"
    if card.skills:
        text += " Skills: " + "; ".join(f"{s.name}: {s.description}" for s in card.skills)
    if len(text) > MAX_CARD_TEXT_CHARS:
        text = text[:MAX_CARD_TEXT_CHARS] + CARD_TEXT_TRUNCATED
    return text


def create_remote_a2a_agents(peers: List[A2APeerConfig]) -> List["RemoteA2aAgent"]:
    """Create RemoteA2aAgent instances from peer configurations."""
    if not A2A_AVAILABLE:
        logger.warning("RemoteA2aAgent not available. Returning empty list.")
        return []

    agents = []
    for peer in peers:
        try:
            agent_card_url = resolve_agent_card_url(peer)
        except Exception as e:
            logger.error(f"Failed to create RemoteA2aAgent for {peer.name}: {e}")
            continue
        # Fetched for every peer: its description and skills go into the sub-agent's description
        card, fetch_error = None, None
        try:
            card = fetch_agent_card(agent_card_url)
        except Exception as e:
            fetch_error = type(e).__name__
            logger.info(f"A2A peer {peer.name}: card not fetched ({fetch_error}); description from the config only")
        # Outside the try below: an unreadable public key stops startup instead of dropping the peer
        status = check_card_signature(card, peer.card_public_key_file, fetch_error)
        log = logger.warning if status.startswith("card signature: invalid") else logger.info
        log(f"A2A peer {peer.name}: {status}")
        try:
            # The signature status first, so the model reads it before what the card claims
            description = f"{status}. Remote agent '{peer.name}' at {peer.url}."
            if peer.capabilities:
                description += f" Capabilities: {', '.join(peer.capabilities)}."
            if text := card_text(card):
                description += " " + text

            # ADK still gets the URL: its own https / same-origin checks stay.
            # A stable per-peer context_id is derived from the DAK session; the raw session_id is not sent (#767)
            agent = RemoteA2aAgent(
                name=peer.name,
                agent_card=agent_card_url,
                description=description,
                config=A2aRemoteAgentConfig(forward_session_id_as_context_id="namespaced"),
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
