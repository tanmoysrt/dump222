from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, FastAPI, HTTPException, Query

from .auth import AuthzCache, CheckRequest, JWKSManager
from .config import ConfigHolder
from .agent_discovery import AgentDiscovery
from .proxy_client import ProxyClient
from .proxy_app import ProxyRouter
from .routes import RouteRegistry

log = logging.getLogger(__name__)


class AdminRouter:
    def __init__(self, cfg_holder: ConfigHolder, discovery: AgentDiscovery) -> None:
        self._cfg_holder = cfg_holder
        self._discovery = discovery
        self._cache = AuthzCache()
        self.router = APIRouter()
        self.router.add_api_route("/_admin/refresh", self.refresh, methods=["POST"])
        self.router.add_api_route(
            "/_admin/check-permission", self.check, methods=["POST"]
        )

    async def refresh(self, agent: str | None = Query(default=None)) -> dict[str, Any]:
        if agent is None:
            await self._discovery.refresh_all()
            return {"refreshed": "all"}
        agent = Path(agent).stem
        if not agent:
            raise HTTPException(status_code=400, detail="invalid agent name")
        ok = await self._discovery.refresh_agent(agent)
        if not ok:
            raise HTTPException(status_code=404, detail=f"unknown agent {agent}")
        return {"refreshed": agent}

    async def check(self, req: CheckRequest) -> dict[str, bool]:
        cached = await self._cache.get(req)
        if cached is not None:
            return {"allowed": cached}
        authz_cfg = self._cfg_holder.current.authz
        if authz_cfg is None:
            raise HTTPException(status_code=503, detail="authz not configured")
        try:
            async with httpx.AsyncClient(timeout=5.0) as c:
                r = await c.post(authz_cfg.url, json=req.model_dump())
                r.raise_for_status()
                data = r.json()
        except Exception as e:
            log.error("authz server unreachable: %s", e)
            raise HTTPException(status_code=503, detail="authz unavailable")
        allowed = bool(data.get("allowed"))
        await self._cache.put(req, allowed)
        return {"allowed": allowed}


def init_admin_app(
    cfg_holder: ConfigHolder,
    discovery: AgentDiscovery,
    jwks: JWKSManager,
    registry: RouteRegistry,
    proxy: ProxyClient,
) -> FastAPI:
    """Build the FastAPI served on node.sock.

    Includes admin endpoints plus the proxy router in local mode, so that
    agent-to-agent traffic flowing through the unix socket is automatically
    treated as `X-Auth-Source: local` (the local socket is the trust
    boundary, per the design). Admin routes are registered first so they
    win against the proxy catch-all route.
    """
    app = FastAPI()
    admin = AdminRouter(cfg_holder, discovery)
    app.include_router(admin.router)
    local_proxy = ProxyRouter(cfg_holder, jwks, registry, proxy, local=True)
    app.include_router(local_proxy.router)
    return app
