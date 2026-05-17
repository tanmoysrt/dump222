from __future__ import annotations


from fastapi import Request
from typing import Any
from jwt import PyJWK
import jwt


def extract_auth_token(request: Request) -> str | None:
    """Extract an auth token from X-Auth-Token or Authorization headers."""
    token = request.headers.get("x-auth-token")
    if token and token.strip():
        return token.strip()

    auth_header = request.headers.get("authorization")
    if not auth_header:
        return None

    scheme, _, token = auth_header.partition(" ")
    if scheme.lower() in {"bearer", "token"} and token.strip():
        return token.strip()

    return None


def decode_jwt(token: str, key: PyJWK, issuer: str, audience: str) -> dict[str, Any]:
    """Decode and verify a JWT using the given key and claims."""
    return jwt.decode(
        token,
        key.key,
        algorithms=["EdDSA"],
        issuer=issuer,
        audience=audience,
        options={"require": ["exp", "iat", "sub", "jti"]},
    )
