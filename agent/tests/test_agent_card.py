"""Signing the Agent Card DAK serves (PBI #638, docs/architecture/a2a.md §7).

The key pair is made for each test run and written only under `tmp_path`.
"""
import base64
import json
import os
import shutil

import pytest
from a2a.client.card_resolver import parse_agent_card
from a2a.server.request_handlers.response_helpers import agent_card_to_dict
from a2a.utils.signing import create_signature_verifier
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from dak_agent import agent_card

CARD = os.path.join(os.path.dirname(__file__), "..", "dak_agent", "agent.json")
# The shape entrypoint.sh writes in the container (1.0 and 0.3 interfaces)
ENTRYPOINT_CARD = {
    "name": "dak_agent",
    "description": "A helpful assistant powered by the Decentralized Agent Kit.",
    "version": "0.1.0",
    "supportedInterfaces": [
        {"url": "http://localhost:8000/a2a/dak_agent", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"},
        {"url": "http://localhost:8000/a2a/dak_agent", "protocolBinding": "JSONRPC", "protocolVersion": "0.3"},
    ],
    "capabilities": {"streaming": True},
    "defaultInputModes": ["text/plain"],
    "defaultOutputModes": ["text/plain"],
    "skills": [{"description": "d", "id": "dak_agent", "name": "model", "tags": ["llm"]}],
}


@pytest.fixture
def signing_key(tmp_path):
    """(private key PEM path, public key PEM path)."""
    key = ec.generate_private_key(ec.SECP256R1())
    private = tmp_path / "card-signing.pem"
    private.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    public = tmp_path / "card-signing.pub.pem"
    public.write_bytes(key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    return str(private), str(public)


@pytest.fixture
def card_path(tmp_path):
    path = tmp_path / "agent.json"
    shutil.copy(CARD, path)
    return str(path)


def _verify(card, public_path):
    with open(public_path, "rb") as f:
        public_pem = f.read()
    create_signature_verifier(lambda kid, jku: public_pem, ["ES256"])(card)


def _read(path):
    with open(path) as f:
        return parse_agent_card(json.load(f))


def test_signs_the_card_when_a_key_file_is_set(card_path, signing_key):
    private, public = signing_key

    assert agent_card.sign_card_in_place(card_path, private, "test-1") is True

    card = _read(card_path)
    assert len(card.signatures) == 1
    _verify(card, public)
    protected = card.signatures[0].protected
    header = json.loads(base64.urlsafe_b64decode(protected + "=" * (-len(protected) % 4)))
    assert header == {"kid": "test-1", "alg": "ES256", "typ": "JOSE"}


def test_leaves_the_card_alone_without_a_key(card_path):
    with open(card_path) as f:
        before = f.read()

    assert agent_card.sign_card_in_place(card_path, None, "test-1") is False
    assert agent_card.sign_card_in_place(card_path, "", "test-1") is False

    with open(card_path) as f:
        assert f.read() == before


def test_a_signed_card_still_verifies_after_the_0_3_compat_fields_are_added(tmp_path, signing_key):
    """ADK serves the card through `agent_card_to_dict`, which adds the 0.3 fields
    when the card has a 0.3 interface, as the one entrypoint.sh writes does."""
    private, public = signing_key
    card_path = tmp_path / "agent.json"
    card_path.write_text(json.dumps(ENTRYPOINT_CARD))
    agent_card.sign_card_in_place(str(card_path), private, "test-1")

    served = agent_card_to_dict(_read(card_path))
    assert "url" in served  # the 0.3 field really is there

    _verify(parse_agent_card(json.loads(json.dumps(served))), public)


def test_an_unreadable_key_file_raises(card_path, tmp_path):
    with pytest.raises(FileNotFoundError):
        agent_card.sign_card_in_place(card_path, str(tmp_path / "missing.pem"), "test-1")

    not_pem = tmp_path / "not.pem"
    not_pem.write_text("not a key")
    with pytest.raises(ValueError):
        agent_card.sign_card_in_place(card_path, str(not_pem), "test-1")


def test_sign_card_from_env_uses_the_agent_name_as_the_default_kid(card_path, signing_key, monkeypatch):
    private, public = signing_key
    monkeypatch.setenv(agent_card.SIGNING_KEY_FILE_ENV, private)
    monkeypatch.delenv(agent_card.SIGNING_KID_ENV, raising=False)
    monkeypatch.setenv("AGENT_NAME", "dak_peer")

    assert agent_card.sign_card_from_env(card_path) is True

    protected = _read(card_path).signatures[0].protected
    header = json.loads(base64.urlsafe_b64decode(protected + "=" * (-len(protected) % 4)))
    assert header["kid"] == "dak_peer"


def test_signing_twice_keeps_one_signature(card_path, signing_key):
    """A restart that does not rewrite agent.json (no entrypoint.sh) signs the signed card again."""
    private, public = signing_key
    agent_card.sign_card_in_place(card_path, private, "test-1")
    agent_card.sign_card_in_place(card_path, private, "test-1")

    card = _read(card_path)
    assert len(card.signatures) == 1
    _verify(card, public)


def _skill(root, name, description):
    (root / name).mkdir(parents=True)
    (root / name / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\ntools: []\n---\nDo it.\n")


def test_card_lists_the_loaded_skills(card_path, tmp_path):
    first, second = tmp_path / "skills", tmp_path / "provider_skills"
    _skill(first, "zeta", "Last by name.")
    _skill(second, "alpha", "First by name.")

    assert agent_card.add_skills_in_place(card_path, [str(first), str(second)]) == 2

    with open(card_path) as f:
        skills = json.load(f)["skills"]
    assert skills == [
        {"id": "alpha", "name": "alpha", "description": "First by name.", "tags": ["skill"]},
        {"id": "zeta", "name": "zeta", "description": "Last by name.", "tags": ["skill"]},
    ]
    assert len(_read(card_path).skills) == 2  # still a card a2a-sdk reads


def test_card_keeps_the_default_skill_without_skills(card_path, tmp_path):
    with open(card_path) as f:
        before = f.read()
    (tmp_path / "empty").mkdir()

    assert agent_card.add_skills_in_place(card_path, [str(tmp_path / "empty")]) == 0

    with open(card_path) as f:
        assert f.read() == before


def test_signature_covers_the_skills(card_path, tmp_path, signing_key):
    private, public = signing_key
    _skill(tmp_path / "skills", "alpha", "a")
    _skill(tmp_path / "skills", "beta", "b")
    agent_card.add_skills_in_place(card_path, [str(tmp_path / "skills")])
    agent_card.sign_card_in_place(card_path, private, "test-1")

    card = _read(card_path)
    _verify(card, public)

    del card.skills[1]
    with pytest.raises(Exception):
        _verify(card, public)
