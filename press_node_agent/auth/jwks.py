from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

import httpx
from jwt import PyJWK

from ..config import Config, ConfigManager

log = logging.getLogger(__name__)


class JWKSManager:
    def __init__(self, config_manager: ConfigManager) -> None:
        cfg = config_manager.current
        self._url = cfg.jwks.url
        self._ttl = cfg.jwks.cache_ttl_seconds
        self._keys: dict[str, PyJWK] = {}
        self._fetched_at: float = 0.0
        self._generation: int = 0
        self._fetch_lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._client = httpx.AsyncClient(timeout=5.0)
        config_manager.subscribe(self._on_config_change)

    async def get_signing_key(self, kid: str) -> PyJWK:
        """Return the signing key for the given kid, refreshing if needed."""
        if kid in self._keys and (time.monotonic() - self._fetched_at) < self._ttl:
            return self._keys[kid]

        await self._refresh()

        if kid not in self._keys:
            self.invalidate()
            await self._refresh()

        if kid not in self._keys:
            raise KeyError(f"kid {kid} not found in JWKS")

        return self._keys[kid]

    def invalidate(self) -> None:
        """Force a refresh on the next get_signing_key call."""
        self._fetched_at = 0.0
        self._generation += 1

    def update_config(self, url: str, ttl: int) -> None:
        """Update the JWKS URL or TTL and invalidate the cached keys."""
        if url != self._url or ttl != self._ttl:
            self._url = url
            self._ttl = ttl
            self._fetched_at = 0.0
            self._generation += 1
            old_client = self._client
            self._client = httpx.AsyncClient(timeout=5.0)
            try:
                asyncio.get_running_loop().create_task(old_client.aclose())
            except RuntimeError:
                pass

    async def refresh_keys_in_background(self) -> None:
        """Periodically refresh the JWKS until stop() is called."""
        while not self._stop.is_set():
            with contextlib.suppress(Exception):
                # Background refresh failures should not kill the task;
                await self._refresh()

            with contextlib.suppress(asyncio.TimeoutError):
                # Refresh halfway through TTL, but never more frequently than 30s.
                await asyncio.wait_for(
                    self._stop.wait(), timeout=max(30, self._ttl // 2)
                )

    async def close(self) -> None:
        """Clean up the HTTP client."""
        self.stop()
        await self._client.aclose()

    def stop(self) -> None:
        """Signal the background refresh loop to exit."""
        self._stop.set()

    async def _refresh(self) -> None:
        """Fetch and parse the JWKS from the remote endpoint."""
        async with self._fetch_lock:
            if (time.monotonic() - self._fetched_at) < self._ttl and self._keys:
                return

            gen = self._generation
            url = self._url
            last_err: Exception | None = None

            for attempt in range(4):
                try:
                    response = await self._client.get(url)
                    response.raise_for_status()
                    data: dict[str, Any] = response.json()

                    keys: dict[str, PyJWK] = {}
                    for jwk in data.get("keys", []):
                        kid = jwk.get("kid")
                        if not kid:
                            continue
                        keys[kid] = PyJWK.from_dict(jwk)

                    if self._generation == gen:
                        self._keys = keys
                        self._fetched_at = time.monotonic()
                    return

                except Exception as e:
                    last_err = e
                    await asyncio.sleep(0.2 * (attempt + 1))

            if not self._stop.is_set():
                log.error("JWKS fetch failed after retries: %s", last_err)
            raise RuntimeError("JWKS unavailable") from last_err

    def _on_config_change(self, old: Config, new: Config) -> None:
        self.update_config(new.jwks.url, new.jwks.cache_ttl_seconds)
