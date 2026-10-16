import logging

from dak_agent.a2a_peer_manager import A2APeerConfig, load_a2a_peers_from_config, resolve_agent_card_url


def test_card_url_wins():
    peer = A2APeerConfig("peer", "http://peer:8000/a2a/dak_agent", card_url="http://cards.example/peer.json")
    assert resolve_agent_card_url(peer) == "http://cards.example/peer.json"


def test_endpoint_url_gets_the_well_known_card_path():
    """The peer's name is not assumed: whatever endpoint the url names, its card is under it."""
    assert resolve_agent_card_url(A2APeerConfig("peer", "http://peer:8000/a2a/dak_peer/")) == \
        "http://peer:8000/a2a/dak_peer/.well-known/agent-card.json"


def test_card_url_given_as_url_is_used_as_is():
    url = "http://peer:8000/a2a/dak_peer/.well-known/agent-card.json"
    assert resolve_agent_card_url(A2APeerConfig("peer", url)) == url


def test_bare_host_keeps_the_old_default_with_a_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="dak_agent.a2a_peer_manager"):
        url = resolve_agent_card_url(A2APeerConfig("peer", "http://agent-provider:8000"))
    assert url == "http://agent-provider:8000/a2a/dak_agent/.well-known/agent-card.json"
    assert "/a2a/dak_agent" in caplog.text and "card_url" in caplog.text


def test_card_url_is_read_from_the_config(tmp_path):
    config = tmp_path / "agent_config.yaml"
    config.write_text(
        "a2a_peers:\n"
        "  - name: peer\n"
        "    url: https://peer.example/a2a/dak_agent\n"
        "    card_url: https://cards.example/peer.json\n"
        "  - name: other\n"
        "    url: https://other.example/a2a/dak_agent\n")
    peer, other = load_a2a_peers_from_config(str(config))
    assert peer.card_url == "https://cards.example/peer.json"
    assert other.card_url is None


# --- the peer's card signature (PBI #638, docs/architecture/a2a.md §7) ---
# Keys are made for each test run and written only under tmp_path.

import json  # noqa: E402
import os  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402
from a2a.client.card_resolver import parse_agent_card  # noqa: E402
from a2a.types import AgentCard  # noqa: E402
from a2a.utils.signing import create_agent_card_signer  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

from dak_agent import a2a_peer_manager  # noqa: E402
from dak_agent.a2a_peer_manager import (  # noqa: E402
    MAX_CARD_TEXT_CHARS,
    check_card_signature,
    create_remote_a2a_agents,
)

CARD = os.path.join(os.path.dirname(__file__), "..", "dak_agent", "agent.json")


def _key_pair(tmp_path, name):
    key = ec.generate_private_key(ec.SECP256R1())
    private = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    public = tmp_path / f"{name}.pub.pem"
    public.write_bytes(key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    return private, str(public)


def _card() -> AgentCard:
    with open(CARD) as f:
        return parse_agent_card(json.load(f))


def _signed(private, kid="peer-1") -> AgentCard:
    return create_agent_card_signer(private, {"kid": kid, "alg": "ES256", "typ": "JOSE"})(_card())


@pytest.fixture
def keys(tmp_path):
    """(private key PEM of the peer, path of its public key PEM)."""
    return _key_pair(tmp_path, "peer")


def test_signature_status_verified(keys):
    private, public = keys
    assert check_card_signature(_signed(private), public, None) == "card signature: verified (kid=peer-1)"


def test_signature_status_unsigned(keys):
    _, public = keys
    assert check_card_signature(_card(), public, None) == "card signature: unsigned"


def test_signature_status_invalid_for_a_tampered_card(keys):
    private, public = keys
    card = _signed(private)
    card.name = card.name[:-1] + "X"
    assert check_card_signature(card, public, None) == "card signature: invalid (InvalidSignaturesError)"


def test_signature_status_invalid_for_another_key(keys, tmp_path):
    _, public = keys
    other_private, _ = _key_pair(tmp_path, "other")
    assert check_card_signature(_signed(other_private), public, None) == \
        "card signature: invalid (InvalidSignaturesError)"


def test_signature_status_invalid_for_a_broken_signature(keys):
    """A protected header that is not base64 JSON fails outside PyJWT's errors; still invalid."""
    private, public = keys
    card = _signed(private)
    card.signatures[0].protected = "!!"
    status = check_card_signature(card, public, None)
    assert status.startswith("card signature: invalid (")
    assert status != "card signature: invalid (InvalidSignaturesError)"


def test_signature_status_unchecked_without_a_key(keys):
    private, _ = keys
    assert check_card_signature(_signed(private), None, None) == \
        "card signature: unchecked (no card_public_key_file)"


def test_signature_status_unchecked_when_the_card_is_not_fetched(keys, monkeypatch):
    _, public = keys

    def refuse(url, timeout=10.0):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(a2a_peer_manager, "fetch_agent_card", refuse)
    peer = A2APeerConfig("peer", "http://peer:8000/a2a/dak_agent", card_public_key_file=public)
    (agent,) = create_remote_a2a_agents([peer])
    assert agent.description.startswith("card signature: unchecked (card not fetched: ConnectError).")


def test_signature_status_unreadable_public_key_raises(keys, tmp_path, monkeypatch):
    """A configuration mistake stops startup; the peer is not silently left out."""
    private, _ = keys
    missing = str(tmp_path / "missing.pub.pem")
    with pytest.raises(FileNotFoundError):
        check_card_signature(_signed(private), missing, None)

    monkeypatch.setattr(a2a_peer_manager, "fetch_agent_card", lambda url, timeout=10.0: _signed(private))
    with pytest.raises(FileNotFoundError):
        create_remote_a2a_agents([A2APeerConfig("peer", "http://peer:8000/a2a/dak_agent", card_public_key_file=missing)])


def test_invalid_peer_is_still_a_sub_agent(keys, tmp_path, monkeypatch, caplog):
    """The result is for the agent to read; the system does not drop the peer (#318)."""
    _, public = keys
    other_private, _ = _key_pair(tmp_path, "other")
    monkeypatch.setattr(a2a_peer_manager, "fetch_agent_card", lambda url, timeout=10.0: _signed(other_private))
    peer = A2APeerConfig("peer", "http://peer:8000/a2a/dak_agent", ["search"], card_public_key_file=public)

    with caplog.at_level(logging.WARNING, logger="dak_agent.a2a_peer_manager"):
        (agent,) = create_remote_a2a_agents([peer])

    assert agent.name == "peer"
    assert agent.description.startswith("card signature: invalid (InvalidSignaturesError). Remote agent 'peer'")
    assert "Capabilities: search" in agent.description
    assert "card signature: invalid" in caplog.text


def test_verified_peer_description_and_card_url(keys, monkeypatch):
    """The card is fetched from the resolved card URL, and RemoteA2aAgent still gets that URL."""
    private, public = keys
    fetched = []
    monkeypatch.setattr(a2a_peer_manager, "fetch_agent_card",
                        lambda url, timeout=10.0: fetched.append(url) or _signed(private))
    peer = A2APeerConfig("peer", "http://peer:8000/a2a/dak_peer", card_public_key_file=public)

    (agent,) = create_remote_a2a_agents([peer])

    assert fetched == ["http://peer:8000/a2a/dak_peer/.well-known/agent-card.json"]
    assert agent._agent_card_source == fetched[0]
    assert agent.description.splitlines()[0].startswith("card signature: verified (kid=peer-1).")


def test_card_public_key_file_is_read_from_the_config(tmp_path):
    config = tmp_path / "agent_config.yaml"
    config.write_text(
        "a2a_peers:\n"
        "  - name: peer\n"
        "    url: https://peer.example/a2a/dak_agent\n"
        "    card_public_key_file: /keys/peer.pub.pem\n"
        "  - name: other\n"
        "    url: https://other.example/a2a/dak_agent\n")
    peer, other = load_a2a_peers_from_config(str(config))
    assert peer.card_public_key_file == "/keys/peer.pub.pem"
    assert other.card_public_key_file is None


def test_a_peer_without_a_url_is_skipped_and_the_others_stay(caplog, monkeypatch):
    """One broken peer entry is logged and left out, as before; it does not stop startup."""
    monkeypatch.setattr(a2a_peer_manager, "fetch_agent_card", lambda url, timeout=10.0: _card())
    broken = A2APeerConfig("broken", None)
    good = A2APeerConfig("good", "http://peer:8000/a2a/dak_agent")

    with caplog.at_level(logging.ERROR, logger="dak_agent.a2a_peer_manager"):
        agents = create_remote_a2a_agents([broken, good])

    assert [a.name for a in agents] == ["good"]
    assert "broken" in caplog.text


# --- the peer's card description and skills (PBI #639, docs/architecture/a2a.md §7) ---

from a2a.types import AgentSkill  # noqa: E402


def _card_with_skills(description="Answers questions about the weather.") -> AgentCard:
    card = _card()
    card.description = description
    del card.skills[:]
    card.skills.extend([
        AgentSkill(id="forecast", name="forecast", description="Tomorrow's weather.", tags=["skill"]),
        AgentSkill(id="premium_service", name="premium_service", description="A paid report.", tags=["skill"]),
    ])
    return card


def test_card_description_includes_the_card_and_its_skills(keys, monkeypatch):
    private, public = keys
    signed = create_agent_card_signer(private, {"kid": "peer-1", "alg": "ES256", "typ": "JOSE"})(_card_with_skills())
    monkeypatch.setattr(a2a_peer_manager, "fetch_agent_card", lambda url, timeout=10.0: signed)
    peer = A2APeerConfig("peer", "http://peer:8000/a2a/dak_agent", ["search"], card_public_key_file=public)

    (agent,) = create_remote_a2a_agents([peer])

    # The signature status, then the config, then what the card says
    assert agent.description == (
        "card signature: verified (kid=peer-1). Remote agent 'peer' at http://peer:8000/a2a/dak_agent."
        " Capabilities: search."
        " Card: Answers questions about the weather."
        " Skills: forecast: Tomorrow's weather.; premium_service: A paid report.")


def test_remote_agent_uses_namespaced_context_id(monkeypatch):
    monkeypatch.setattr(a2a_peer_manager, "fetch_agent_card", lambda url, timeout=10.0: _card_with_skills())
    (agent,) = create_remote_a2a_agents([A2APeerConfig("peer", "http://peer:8000/a2a/dak_agent")])

    assert agent._config.forward_session_id_as_context_id == "namespaced"


def test_card_description_is_truncated_at_1024(monkeypatch):
    monkeypatch.setattr(a2a_peer_manager, "fetch_agent_card", lambda url, timeout=10.0: _card_with_skills("x" * 2000))
    (agent,) = create_remote_a2a_agents([A2APeerConfig("peer", "http://peer:8000/a2a/dak_agent")])

    text = agent.description[agent.description.index("Card: "):]
    assert text == ("Card: " + "x" * 2000)[:MAX_CARD_TEXT_CHARS] + " …(card text truncated)"
    assert "premium_service" not in agent.description


def test_card_description_is_config_only_when_the_card_is_not_fetched(monkeypatch):
    def refuse(url, timeout=10.0):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(a2a_peer_manager, "fetch_agent_card", refuse)
    (agent,) = create_remote_a2a_agents([A2APeerConfig("peer", "http://peer:8000/a2a/dak_agent", ["search"])])

    assert agent.description == (
        "card signature: unchecked (no card_public_key_file). Remote agent 'peer' at http://peer:8000/a2a/dak_agent."
        " Capabilities: search.")


def test_capabilities_can_be_omitted(monkeypatch):
    monkeypatch.setattr(a2a_peer_manager, "fetch_agent_card", lambda url, timeout=10.0: _card_with_skills())
    (agent,) = create_remote_a2a_agents([A2APeerConfig("peer", "http://peer:8000/a2a/dak_agent")])

    assert "Capabilities:" not in agent.description
    assert "general purpose" not in agent.description
    assert "Skills: forecast: Tomorrow's weather." in agent.description
