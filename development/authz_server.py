"""Development auth server — JWKS endpoint + allow-all authz check.

Generates an Ed25519 key pair in development/dev-keys/ on first run.

Usage:
    # start the server (listens on 127.0.0.1:9100)
    python development/authz_server.py
    python development/authz_server.py serve --port 9100

    # mint a signed JWT
    python development/authz_server.py mint --sub alice --roles admin,viewer
    python development/authz_server.py mint --sub alice --ttl 7200 --roles admin

Endpoints:
    GET  /.well-known/jwks.json  — current public key as JWK set
    POST /check                  — logs and allows every authz request
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
import uuid
from pathlib import Path

import uvicorn
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
    load_pem_private_key,
)
from fastapi import FastAPI, Request

logging.basicConfig(level=logging.INFO, format="%(asctime)s authz %(message)s")
log = logging.getLogger("authz")

_KEYS_DIR = Path(__file__).parent / "dev-keys"
_PRIVATE_PEM = _KEYS_DIR / "private.pem"
_PUBLIC_PEM = _KEYS_DIR / "public.pem"
KID = "dev-key-1"


def _ensure_keys() -> Ed25519PrivateKey:
    """Return the dev private key, generating a new pair if none exists."""
    if _PRIVATE_PEM.exists():
        private_key = load_pem_private_key(_PRIVATE_PEM.read_bytes(), password=None)
        log.info("Loaded dev key from %s", _PRIVATE_PEM)
    else:
        _KEYS_DIR.mkdir(parents=True, exist_ok=True)
        private_key = Ed25519PrivateKey.generate()
        _PRIVATE_PEM.write_bytes(
            private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
        )
        _PUBLIC_PEM.write_bytes(
            private_key.public_key().public_bytes(
                Encoding.PEM, PublicFormat.SubjectPublicKeyInfo
            )
        )
        log.info("Generated new Ed25519 dev key → %s", _KEYS_DIR)
    return private_key  # type: ignore[return-value]


def _public_jwk(private_key: Ed25519PrivateKey) -> dict:
    """Return the public key encoded as an OKP JWK (Ed25519)."""
    import base64

    raw = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    x = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    return {"kty": "OKP", "crv": "Ed25519", "kid": KID, "use": "sig", "x": x}


# Initialise once so both the server and the CLI share the same key.
_private_key: Ed25519PrivateKey = _ensure_keys()
_jwks_response: dict = {"keys": [_public_jwk(_private_key)]}


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI()


@app.get("/.well-known/jwks.json")
async def jwks() -> dict:
    return _jwks_response


@app.post("/check")
async def check(req: Request) -> dict[str, bool]:
    body = await req.json()
    log.info(
        "ALLOW sub=%s jti=%s resource=%s action=%s",
        body.get("sub"),
        body.get("jti"),
        body.get("resource"),
        body.get("action"),
    )
    return {"allowed": True}


def _mint(sub: str, roles: list[str], ttl: int, issuer: str, audience: str) -> str:
    """Return a signed EdDSA JWT for the given subject."""
    import jwt  # PyJWT

    now = int(time.time())
    payload = {
        "sub": sub,
        "iss": issuer,
        "aud": audience,
        "iat": now,
        "exp": now + ttl,
        "jti": str(uuid.uuid4()),
        "roles": roles,
    }
    private_pem = _private_key.private_bytes(
        Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
    )
    return jwt.encode(payload, private_pem, algorithm="EdDSA", headers={"kid": KID})


def _cli() -> None:
    parser = argparse.ArgumentParser(
        prog="authz_server.py",
        description="Dev auth server and JWT minter.",
    )
    cmds = parser.add_subparsers(dest="cmd")

    serve_p = cmds.add_parser("serve", help="Run the dev auth server (default)")
    serve_p.add_argument("--host", default="127.0.0.1")
    serve_p.add_argument("--port", type=int, default=9100)

    mint_p = cmds.add_parser("mint", help="Mint a signed JWT and print it")
    mint_p.add_argument("--sub", required=True, help="Subject (required)")
    mint_p.add_argument(
        "--roles", default="", help="Comma-separated roles (e.g. admin,viewer)"
    )
    mint_p.add_argument(
        "--ttl", type=int, default=3600, help="Token lifetime in seconds (default 3600)"
    )
    mint_p.add_argument("--issuer", default="press-controlplane")
    mint_p.add_argument("--audience", default="press-node")

    args = parser.parse_args()

    if args.cmd == "mint":
        roles = [r.strip() for r in args.roles.split(",") if r.strip()]
        print(_mint(args.sub, roles, args.ttl, args.issuer, args.audience))
        sys.exit(0)

    host = getattr(args, "host", "127.0.0.1")
    port = getattr(args, "port", 9100)
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    _cli()
