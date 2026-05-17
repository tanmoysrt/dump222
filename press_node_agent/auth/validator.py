from __future__ import annotations

from typing import Any

import jwt
from fastapi import Request

from press_node_agent.helpers import decode_jwt, extract_auth_token

from .jwks import JWKSManager


class AuthResult:
    __slots__ = ("source", "sub", "roles", "jti")

    def __init__(
        self, source: str, sub: str = "", roles: str = "", jti: str = ""
    ) -> None:
        """Store the authentication context from the decoded JWT."""
        self.source = source
        self.sub = sub
        self.roles = roles
        self.jti = jti

    def headers(self) -> dict[str, str]:
        """Return the X-Auth-* headers to forward to the upstream agent."""
        if self.source == "local":
            return {"X-Auth-Source": "local"}
        return {
            "X-Auth-Source": self.source,
            "X-Auth-Sub": self.sub,
            "X-Auth-Roles": self.roles,
            "X-Auth-Jti": self.jti,
        }


class AuthError(Exception):
    def __init__(self, status: int, msg: str) -> None:
        """Create an auth error with an HTTP status code and message."""
        self.status = status
        self.msg = msg
        super().__init__(msg)


async def validate_request(
    request: Request, jwks_manager: JWKSManager, issuer: str, audience: str
) -> AuthResult:
    """Validate the request JWT and return the extracted auth context."""
    token = extract_auth_token(request)
    if not token:
        raise AuthError(401, "missing token")

    try:
        header = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError:
        raise AuthError(401, "invalid token header")

    kid = header.get("kid")
    if not kid:
        raise AuthError(401, "missing kid")

    try:
        key = await jwks_manager.get_signing_key(kid)
    except KeyError:
        raise AuthError(401, "unknown kid")
    except RuntimeError:
        raise AuthError(503, "jwks unavailable")

    try:
        payload: dict[str, Any] = decode_jwt(token, key, issuer, audience)
    except jwt.ExpiredSignatureError:
        raise AuthError(401, "token expired")
    except jwt.InvalidSignatureError:
        jwks_manager.invalidate()
        try:
            key = await jwks_manager.get_signing_key(kid)
            payload = decode_jwt(token, key, issuer, audience)
        except Exception:
            raise AuthError(401, "invalid signature")
    except jwt.InvalidTokenError as e:
        raise AuthError(401, f"invalid token: {e}")

    roles = payload.get("roles") or []
    if isinstance(roles, list):
        roles_str = ",".join(str(r) for r in roles)
    else:
        roles_str = str(roles)
    return AuthResult(
        source="external",
        sub=str(payload.get("sub", "")),
        roles=roles_str,
        jti=str(payload.get("jti", "")),
    )
