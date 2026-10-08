"""Spike (PBI #318 / Task #320): sign DAK's Agent Card with a2a-sdk 1.x and verify it.

Fetches the card DAK serves (or reads one from a file), signs it with `create_agent_card_signer`
(JWS over the JCS-canonicalized card, A2A 1.0 §8.4), verifies it with `create_signature_verifier`,
and shows that changing one character of the signed card makes verification fail. The key pair
is made in memory for this run and never written anywhere.

Not production code. Run it with the agent's environment, as README.md in this directory says.
"""
import argparse
import asyncio
import json

import httpx
from a2a.client.card_resolver import A2ACardResolver, parse_agent_card
from a2a.types import AgentCard
from a2a.utils.signing import (
    NoSignatureError,
    SignatureVerificationError,
    create_agent_card_signer,
    create_signature_verifier,
)
from cryptography.hazmat.primitives.asymmetric import ec
from google.protobuf.json_format import MessageToDict

KID = "dak-spike-1"


async def fetch_card(base_url: str, verifier=None) -> AgentCard:
    async with httpx.AsyncClient(timeout=10) as http:
        return await A2ACardResolver(http, base_url).get_agent_card(signature_verifier=verifier)


def copy_of(card: AgentCard) -> AgentCard:
    copy = AgentCard()
    copy.CopyFrom(card)
    return copy


def check(label: str, verify, card: AgentCard) -> None:
    try:
        verify(card)
        print(f"{label}: verified")
    except SignatureVerificationError as e:
        print(f"{label}: {type(e).__name__}: {e}")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000/a2a/dak_agent")
    parser.add_argument("--card-file", help="read the card from this file instead of fetching it")
    args = parser.parse_args()

    private_key = ec.generate_private_key(ec.SECP256R1())
    public_key = private_key.public_key()
    sign = create_agent_card_signer(private_key, {"kid": KID, "alg": "ES256", "typ": "JOSE"})
    verify = create_signature_verifier(lambda kid, jku: public_key if kid == KID else None, ["ES256"])

    if args.card_file:
        with open(args.card_file) as f:
            card = parse_agent_card(json.load(f))
    else:
        # A Consumer that asks a2a-sdk to verify DAK's card as served today gets NoSignatureError.
        try:
            await fetch_card(args.base_url, verify)
        except NoSignatureError as e:
            print(f"served card with verifier: NoSignatureError: {e}")
        card = await fetch_card(args.base_url)
    print(f"card: name={card.name} signatures={len(card.signatures)}")

    signed = sign(copy_of(card))  # the signer appends to the card it is given
    print(f"signed: signatures={len(signed.signatures)} protected={signed.signatures[0].protected[:24]}...")
    check("signed card", verify, signed)

    # Round trip through JSON, as a peer would receive it.
    received = parse_agent_card(json.loads(json.dumps(MessageToDict(signed))))
    check("signed card after JSON round trip", verify, received)

    tampered = copy_of(received)
    tampered.name = tampered.name[:-1] + ("X" if tampered.name[-1] != "X" else "Y")
    check(f"tampered card (name={tampered.name})", verify, tampered)

    other_key = ec.generate_private_key(ec.SECP256R1())
    forged = create_agent_card_signer(other_key, {"kid": KID, "alg": "ES256", "typ": "JOSE"})(copy_of(card))
    check("card signed by another key with the same kid", verify, forged)


if __name__ == "__main__":
    asyncio.run(main())
