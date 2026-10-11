"""Make the Agent Card signing key pair for the fake-LLM stack (PBI #638).

Run by the `card-keygen` service in docker-compose.test.yml each time the stack
comes up: writes `card-signing.pem` (EC P-256, PKCS8, unencrypted) and
`card-signing.pub.pem` into the directory given, unless they are already there.
No key is kept in the repository; the directory is gitignored.

    python make_card_keys.py <dir>
"""
import os
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def main(out_dir: str) -> None:
    private_path = os.path.join(out_dir, "card-signing.pem")
    public_path = os.path.join(out_dir, "card-signing.pub.pem")
    if os.path.exists(private_path) and os.path.exists(public_path):
        print(f"card keys already in {out_dir}")
        return
    os.makedirs(out_dir, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    with open(private_path, "wb") as f:
        f.write(key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    with open(public_path, "wb") as f:
        f.write(key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    print(f"card keys written to {out_dir}")


if __name__ == "__main__":
    main(sys.argv[1])
