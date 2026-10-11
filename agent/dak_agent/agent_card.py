"""Sign the Agent Card DAK serves (PBI #638, docs/architecture/a2a.md §7).

`entrypoint.sh` writes `agent.json`; ADK reads it when the app is built and
serves it at `/a2a/dak_agent/.well-known/agent-card.json`. When a signing key
is set, the card gets one JWS signature (A2A 1.0 §8.4, ES256) before ADK reads
it. Without a key the card is left as it is.

Before signing, the skills the agent loads (`AGENT_SKILLS_DIRS`) replace the
card's `skills`, so a peer reading the card sees what this agent can do (PBI #639).
"""
import json
import logging
import os

from a2a.client.card_resolver import parse_agent_card
from a2a.utils.signing import create_agent_card_signer
from google.protobuf.json_format import MessageToDict

from .skill_registry import SkillRegistry

logger = logging.getLogger(__name__)

SIGNING_KEY_FILE_ENV = "AGENT_CARD_SIGNING_KEY_FILE"
SIGNING_KID_ENV = "AGENT_CARD_SIGNING_KID"
SIGNING_ALG = "ES256"


def add_skills_in_place(path: str, skills_dirs: list[str]) -> int:
    """List the skills found in `skills_dirs` as the card's `skills`, by name.

    No skills: the card keeps the skill entrypoint.sh wrote and 0 is returned.
    """
    registry = SkillRegistry(skills_dirs)
    registry.load_skills()
    skills = sorted(registry.list_skills(), key=lambda s: s["name"])
    if not skills:
        return 0
    with open(path, encoding="utf-8") as f:
        card = json.load(f)
    card["skills"] = [
        {"id": s["name"], "name": s["name"], "description": s["description"], "tags": ["skill"]} for s in skills
    ]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(card, f, indent=4)
    logger.info("Agent Card lists %d skill(s)", len(skills))
    return len(skills)


def sign_card_in_place(path: str, key_file: str | None, kid: str) -> bool:
    """Add a signature to the card at `path` with the PEM private key in `key_file`.

    No key file: nothing is done and False is returned. A key that cannot be
    read raises, so a misconfigured agent stops instead of serving an unsigned card.
    """
    if not key_file:
        logger.info("Agent Card not signed (%s unset)", SIGNING_KEY_FILE_ENV)
        return False
    with open(key_file, "rb") as f:
        pem = f.read()
    with open(path, encoding="utf-8") as f:
        card = parse_agent_card(json.load(f))
    # A restart without entrypoint.sh rewriting the file would otherwise add a second signature
    del card.signatures[:]
    sign = create_agent_card_signer(pem, {"kid": kid, "alg": SIGNING_ALG, "typ": "JOSE"})
    signed = sign(card)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(MessageToDict(signed), f, indent=4)
    logger.info("Agent Card signed (kid=%s)", kid)
    return True


def sign_card_from_env(path: str) -> bool:
    kid = os.getenv(SIGNING_KID_ENV) or os.getenv("AGENT_NAME") or "dak_agent"
    return sign_card_in_place(path, os.getenv(SIGNING_KEY_FILE_ENV), kid)
