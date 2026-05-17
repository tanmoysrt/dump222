from __future__ import annotations

import logging
import re
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query

from ..auth import AuthzCache, CheckRequest
from ..config import ConfigManager
from ..discovery import AgentDiscovery

log = logging.getLogger(__name__)


class AdminRouter:
    def __init__(
        self, config_manager: ConfigManager, agent_discovery: AgentDiscovery
    ) -> None:
        self.config_manager = config_manager
        self.agent_discovery = agent_discovery
        self.authz_cache = AuthzCache()
        self._http_client = httpx.AsyncClient(timeout=5.0)
        self.router = APIRouter()
        self.router.add_api_route(
            "/_admin/refresh-agent", self.refresh_agent, methods=["POST"]
        )
        self.router.add_api_route(
            "/_admin/has-permission", self.has_permission, methods=["POST"]
        )

    _AGENT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

    async def refresh_agent(
        self, agent: str | None = Query(default=None)
    ) -> dict[str, Any]:
        """Trigger a route prefix and whitelist refresh for a single agent or all agents."""
        if agent is None:
            await self.agent_discovery.refresh_all()
            return {"refreshed": "all"}

        if not self._AGENT_NAME_RE.match(agent):
            raise HTTPException(status_code=400, detail="invalid agent name")

        ok = await self.agent_discovery.refresh_agent(agent)
        if not ok:
            raise HTTPException(status_code=404, detail=f"unknown agent {agent}")

        return {"refreshed": agent}

    async def has_permission(self, req: CheckRequest) -> dict[str, bool]:
        """Check authorization with the remote authz service, using a local cache."""
        cached = await self.authz_cache.get(req)
        if cached is not None:
            return {"allowed": cached}

        authz_cfg = self.config_manager.current.authz
        if not authz_cfg.url:
            raise HTTPException(status_code=503, detail="authz not configured")

        headers = {}
        if authz_cfg.jwt_token:
            headers["Authorization"] = f"Token {authz_cfg.jwt_token}"

        try:
            r = await self._http_client.post(
                authz_cfg.url, json=req.model_dump(), headers=headers
            )
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            log.error("authz server unreachable: %s", e)
            raise HTTPException(status_code=503, detail="authz unavailable")

        allowed = bool(data.get("allowed"))
        await self.authz_cache.put(req, allowed)
        return {"allowed": allowed}

    async def close(self) -> None:
        """Clean up resources like the HTTP client."""
        await self._http_client.aclose()
