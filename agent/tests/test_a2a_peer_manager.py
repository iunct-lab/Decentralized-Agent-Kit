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
