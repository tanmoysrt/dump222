from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx
import jwt
from fastapi import Request
from jwt import PyJWK
from pydantic import BaseModel

log = logging.getLogger(__name__)


class Resource(BaseModel):
    type: str
    id: str


class CheckRequest(BaseModel):
    sub: str
    jti: str
    resource: Resource
    action: str


class AuthzCache:
    MAX_SIZE = 10_000

    def __init__(self) -> None:
        self._cache: dict[tuple, tuple[bool, float]] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _key(req: CheckRequest) -> tuple:
        return (req.jti, req.sub, req.resource.type, req.resource.id, req.action)

    async def get(self, req: CheckRequest) -> bool | None:
        k = self._key(req)
        async with self._lock:
            hit = self._cache.get(k)
            if hit is None:
                return None
            allowed, exp = hit
            if time.time() >= exp:
                del self._cache[k]
                return None
        return allowed

    async def put(self, req: CheckRequest, allowed: bool) -> None:
        ttl = 60.0 if allowed else 10.0
        k = self._key(req)
        async with self._lock:
            if len(self._cache) >= self.MAX_SIZE and k not in self._cache:
                oldest = next(iter(self._cache))
                del self._cache[oldest]
            self._cache[k] = (allowed, time.time() + ttl)


class JWKSManager:
    def __init__(self, url: str, ttl: int) -> None:
        self._url = url
        self._ttl = ttl
        self._keys: dict[str, PyJWK] = {}
        self._fetched_at: float = 0.0
        self._fetch_lock = asyncio.Lock()
        self._stop = asyncio.Event()

    def update_config(self, url: str, ttl: int) -> None:
        if url != self._url or ttl != self._ttl:
            self._url = url
            self._ttl = ttl
            self._fetched_at = 0.0

    async def get_key(self, kid: str) -> PyJWK:
        if kid in self._keys and (time.time() - self._fetched_at) < self._ttl:
            return self._keys[kid]
        await self._refresh()
        if kid not in self._keys:
            self._fetched_at = 0.0
            await self._refresh()
        if kid not in self._keys:
            raise KeyError(f"kid {kid} not found in JWKS")
        return self._keys[kid]

    async def invalidate(self) -> None:
        self._fetched_at = 0.0

    async def _refresh(self) -> None:
        if (time.time() - self._fetched_at) < self._ttl and self._keys:
            return
        async with self._fetch_lock:
            if (time.time() - self._fetched_at) < self._ttl and self._keys:
                return
            url = self._url
        last_err: Exception | None = None
        for attempt in range(4):
            try:
                async with httpx.AsyncClient(timeout=5.0) as c:
                    r = await c.get(url)
                    r.raise_for_status()
                    data: dict[str, Any] = r.json()
                keys: dict[str, PyJWK] = {}
                for jwk in data.get("keys", []):
                    kid = jwk.get("kid")
                    if not kid:
                        continue
                    keys[kid] = PyJWK.from_dict(jwk)
                self._keys = keys
                self._fetched_at = time.time()
                return
            except Exception as e:
                last_err = e
                await asyncio.sleep(0.2 * (attempt + 1))
        log.error("JWKS fetch failed after retries: %s", last_err)
        raise RuntimeError("JWKS unavailable") from last_err

    async def run_background(self) -> None:
        while not self._stop.is_set():
            try:
                await self._refresh()
            except Exception:
                pass
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=max(30, self._ttl // 2)
                )
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()


def _decode_jwt(token: str, key: PyJWK, issuer: str, audience: str) -> dict[str, Any]:
    return jwt.decode(
        token,
        key.key,
        algorithms=["EdDSA"],
        issuer=issuer,
        audience=audience,
        options={"require": ["exp", "iat", "sub", "jti"]},
    )


class AuthResult:
    __slots__ = ("source", "sub", "roles", "jti")

    def __init__(
        self, source: str, sub: str = "", roles: str = "", jti: str = ""
    ) -> None:
        self.source = source
        self.sub = sub
        self.roles = roles
        self.jti = jti

    def headers(self) -> dict[str, str]:
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
        self.status = status
        self.msg = msg
        super().__init__(msg)


def _extract_token(request: Request) -> str | None:
    auth_header = request.headers.get("authorization")
    if not auth_header:
        return None
    parts = auth_header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() not in ("token", "bearer"):
        return None
    return parts[1].strip()


async def validate_request(
    request: Request, jwks: JWKSManager, issuer: str, audience: str
) -> AuthResult:
    token = _extract_token(request)
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
        key = await jwks.get_key(kid)
    except KeyError:
        raise AuthError(401, "unknown kid")
    except RuntimeError:
        raise AuthError(503, "jwks unavailable")

    try:
        payload: dict[str, Any] = _decode_jwt(token, key, issuer, audience)
    except jwt.ExpiredSignatureError:
        raise AuthError(401, "token expired")
    except jwt.InvalidSignatureError:
        await jwks.invalidate()
        try:
            key = await jwks.get_key(kid)
            payload = _decode_jwt(token, key, issuer, audience)
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
