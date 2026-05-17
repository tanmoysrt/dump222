from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from ..auth import JWKSManager
from ..config import ConfigManager
from ..discovery import AgentDiscovery
from ..proxy_client import ProxyClient
from ..routes import RouteRegistry
from .admin import AdminRouter
from .proxy import ProxyRouter

__all__ = ["create_internal_node_app", "create_proxy_app"]


def create_internal_node_app(
    config_manager: ConfigManager,
    agent_discovery: AgentDiscovery,
    jwks_manager: JWKSManager,
    route_registry: RouteRegistry,
    proxy_client: ProxyClient,
) -> FastAPI:
    """Create the FastAPI app served on node.sock (admin + local proxy)."""
    admin_router = AdminRouter(config_manager, agent_discovery)
    proxy_router = ProxyRouter(
        config_manager,
        jwks_manager,
        route_registry,
        proxy_client,
        is_local=True,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            yield
        finally:
            await admin_router.close()
            await proxy_router.close()

    app = FastAPI(lifespan=lifespan)
    app.include_router(admin_router.router)
    app.include_router(proxy_router.router)
    return app


def create_proxy_app(
    config_manager: ConfigManager,
    jwks_manager: JWKSManager,
    route_registry: RouteRegistry,
    proxy_client: ProxyClient,
) -> FastAPI:
    """Create the FastAPI app that proxies external traffic to agent and controlplane"""
    proxy_router = ProxyRouter(
        config_manager,
        jwks_manager,
        route_registry,
        proxy_client,
        is_local=False,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            yield
        finally:
            await proxy_router.close()

    app = FastAPI(lifespan=lifespan)
    app.include_router(proxy_router.router)
    return app
