from __future__ import annotations

import logging

from fastapi import APIRouter, FastAPI, Request, Response

from .auth import AuthError, AuthResult, JWKSManager, validate_request
from .config import Config, ConfigHolder
from .proxy_client import ProxyClient
from .routes import RouteRegistry

log = logging.getLogger(__name__)


class ProxyRouter:
    def __init__(
        self,
        cfg_holder: ConfigHolder,
        jwks: JWKSManager,
        registry: RouteRegistry,
        proxy: ProxyClient,
        local: bool = False,
    ) -> None:
        self._cfg_holder = cfg_holder
        self._jwks = jwks
        self._registry = registry
        self._proxy = proxy
        self._local = local
        self.router = APIRouter()
        self.router.add_api_route("/_meta/health", self.health, methods=["GET"])
        self.router.add_api_route(
            "/{full_path:path}",
            self.dispatch,
            methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
        )

    async def health(self) -> dict[str, str]:
        return {"status": "ok"}

    async def dispatch(self, full_path: str, request: Request) -> Response:
        path = request.url.path
        route = self._registry.lookup(path)
        if route is None:
            return Response(status_code=404, content=b"not found")

        cfg = self._cfg_holder.current
        if route.dest_type == "controlplane":
            # External callers must present a valid user JWT. Local callers
            # (agents over node.sock) are trusted by the socket boundary and
            # the node attaches its own credentials downstream.
            if not self._local:
                try:
                    await validate_request(
                        request, self._jwks, cfg.jwt.issuer, cfg.jwt.audience
                    )
                except AuthError as e:
                    return Response(status_code=e.status, content=e.msg.encode())
            return await self._proxy.proxy_to_controlplane(request, route)

        if route.dest_type == "authz":
            # Authz is only reachable from the local socket; external callers
            # have no business talking to it directly through the node.
            if not self._local:
                return Response(status_code=404, content=b"not found")
            return await self._proxy.proxy_to_authz(request, route)

        if self._local:
            # Trust boundary is the unix socket. Any caller reaching us here
            # is already inside the node's root-controlled environment.
            auth = AuthResult(source="local")
        elif route.is_whitelisted(path):
            auth = AuthResult(source="external", sub="", roles="", jti="")
        else:
            try:
                auth = await validate_request(
                    request, self._jwks, cfg.jwt.issuer, cfg.jwt.audience
                )
            except AuthError as e:
                return Response(status_code=e.status, content=e.msg.encode())

        return await self._proxy.proxy_to_agent(request, route, auth.headers())


def init_proxy_app(
    cfg_holder: ConfigHolder,
    jwks: JWKSManager,
    registry: RouteRegistry,
    proxy: ProxyClient,
    local: bool = False,
) -> FastAPI:
    app = FastAPI()
    proxy_router = ProxyRouter(cfg_holder, jwks, registry, proxy, local=local)
    app.include_router(proxy_router.router)
    return app


async def apply_config(
    cfg: Config, jwks: JWKSManager, registry: RouteRegistry, proxy: ProxyClient
) -> None:
    jwks.update_config(cfg.jwks.url, cfg.jwks.cache_ttl_seconds)
    await registry.set_controlplane(cfg.controlplane.url, cfg.controlplane.prefixes)
    if cfg.authz is not None:
        await registry.set_authz(cfg.authz.url, cfg.authz.prefixes)
    else:
        await registry.set_authz("", [])
    proxy.set_controlplane(cfg.controlplane.url)
    proxy.set_authz(cfg.authz.url if cfg.authz is not None else None)
    proxy.set_node_jwt(cfg.node_jwt)
