from __future__ import annotations

import logging

from fastapi import APIRouter, Request, Response

from ..auth import AuthError, AuthResult, JWKSManager, validate_request
from ..config import ConfigManager
from ..proxy_client import ProxyClient
from ..routes import RouteRegistry

log = logging.getLogger(__name__)


class ProxyRouter:
    def __init__(
        self,
        config_manager: ConfigManager,
        jwks_manager: JWKSManager,
        route_registry: RouteRegistry,
        proxy_client: ProxyClient,
        is_local: bool = False,
    ) -> None:
        """Initialize the proxy router with config, auth, and routing dependencies."""
        self.config_manager = config_manager
        self.jwks_manager = jwks_manager
        self.route_registry = route_registry
        self.proxy_client = proxy_client
        self.is_local = is_local

        # Create FastAPI router and define routes
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
        """Route the incoming request to the appropriate upstream destination."""
        _ = full_path

        path = request.url.path
        route = self.route_registry.lookup(path)
        if route is None:
            return Response(status_code=404, content=b"not found")

        config = self.config_manager.current
        if route.dest_type == "controlplane":
            # External callers must present a valid user JWT; that token is then
            # forwarded to the controlplane so it sees the original caller's identity.
            # Local callers (agents over node.sock) are trusted by the socket boundary
            # and the node attaches its own credentials downstream.

            if not self.is_local:
                try:
                    await validate_request(
                        request,
                        self.jwks_manager,
                        config.jwt.issuer,
                        config.jwt.audience,
                    )
                except AuthError as e:
                    return Response(status_code=e.status, content=e.msg.encode())

                # Forward the validated user token so the controlplane can identify
                # the original caller.
                user_authorization = request.headers.get("authorization", "")
                return await self.proxy_client.proxy_to_controlplane(
                    request, route, user_authorization=user_authorization
                )

            # Local / agent-initiated: node attaches its own credentials.
            return await self.proxy_client.proxy_to_controlplane(request, route)

        if route.dest_type == "authz":
            # Authz is only reachable from the local socket; external callers
            # have no usecase talking to it directly through the node.
            if not self.is_local:
                return Response(status_code=404, content=b"not found")
            return await self.proxy_client.proxy_to_authz(request)

        if self.is_local:
            # Trust boundary is the unix socket. Any caller reaching us here
            # is already inside the node's trusted boundary.
            auth = AuthResult(source="local")

        elif route.is_whitelisted(path):
            auth = AuthResult(source="external", sub="", roles="", jti="")
        else:
            try:
                auth = await validate_request(
                    request, self.jwks_manager, config.jwt.issuer, config.jwt.audience
                )
            except AuthError as e:
                return Response(status_code=e.status, content=e.msg.encode())

        return await self.proxy_client.proxy_to_agent(request, route, auth.headers())

    async def close(self) -> None:
        pass
