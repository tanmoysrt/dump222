from __future__ import annotations

import asyncio
import time

from press_api_spec.node_agent_service_v1.models import CheckRequest

__all__ = ["CheckRequest", "AuthzCache"]


class AuthzCache:
    MAX_SIZE = 10_000
    EVICT_BATCH = 500
    ALLOW_TTL = 60
    DENY_TTL = 10

    def __init__(self) -> None:
        self._cache: dict[tuple, tuple[bool, float]] = {}
        self._lock = asyncio.Lock()

    async def get(self, req: CheckRequest) -> bool | None:
        key = self._generate_cache_key(req)
        now = time.monotonic()

        async with self._lock:
            hit = self._cache.get(key)
            if hit is None:
                return None

            allowed, exp = hit
            if now >= exp:
                self._cache.pop(key, None)
                return None

            return allowed

    async def put(self, req: CheckRequest, allowed: bool) -> None:
        key = self._generate_cache_key(req)
        ttl = self.ALLOW_TTL if allowed else self.DENY_TTL
        now = time.monotonic()

        async with self._lock:
            if len(self._cache) >= self.MAX_SIZE and key not in self._cache:
                # First reclaim expired entries
                expired = [k for k, (_, exp) in self._cache.items() if exp <= now][
                    : self.EVICT_BATCH
                ]
                for k in expired:
                    self._cache.pop(k, None)

                # If it still full, evict oldest batch
                if len(self._cache) >= self.MAX_SIZE:
                    it = iter(self._cache)
                    for _ in range(min(self.EVICT_BATCH, len(self._cache))):
                        self._cache.pop(next(it), None)

            self._cache[key] = (allowed, now + ttl)

    def _generate_cache_key(self, req: CheckRequest) -> tuple:
        return (req.jti, req.sub, req.resource.type, req.resource.id, req.action)