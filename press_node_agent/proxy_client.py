from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

import httpx
from fastapi import Request, Response
from fastapi.responses import StreamingResponse

from .config import Config, ConfigManager
from .routes import RouteEntry

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0
AGENT_BASE_URL = "http://agent"
NODE_AUTH_HEADER = "authorization"


HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}

SENSITIVE_INBOUND_HEADERS = {
    "authorization",
    "x-auth-token",
    "x-auth-source",
    "x-auth-sub",
    "x-auth-roles",
    "x-auth-jti",
}

MAX_BODY_SIZE = 128 * 1024 * 1024  # 128 MB


class ProxyClient:
    """
    Reusable HTTP proxy helper.

    Maintains long-lived clients for:
    - agents
    - controlplane
    - authz
    """

    def __init__(self, config_manager: ConfigManager) -> None:
        cfg = config_manager.current

        self._agent_clients: dict[str, httpx.AsyncClient] = {}

        # Per-controlplane client and JWT, keyed by controlplane name.
        self._cp_clients: dict[str, httpx.AsyncClient] = {}
        self._cp_jwts: dict[str, str] = {}
        for cp in cfg.controlplanes:
            self._cp_clients[cp.name] = self._create_http_client(cp.url)
            self._cp_jwts[cp.name] = cp.jwt_token

        self._authz_client = (
            self._create_http_client(cfg.authz.url) if cfg.authz.url else None
        )
        self._authz_jwt = cfg.authz.jwt_token

        config_manager.subscribe(self._on_config_changed)

    async def _on_config_changed(self, old: Config, new: Config) -> None:
        """
        Update runtime config without restarting the service.
        """
        new_by_name = {cp.name: cp for cp in new.controlplanes}
        old_by_name = {cp.name: cp for cp in old.controlplanes}

        # Remove controlplanes that disappeared.
        for name in list(self._cp_clients):
            if name not in new_by_name:
                old_client = self._cp_clients.pop(name)
                self._cp_jwts.pop(name, None)
                await old_client.aclose()

        # Add or replace clients for new/changed controlplanes.
        for name, cp in new_by_name.items():
            old_cp = old_by_name.get(name)
            if old_cp is None or old_cp.url != cp.url:
                existing = self._cp_clients.get(name)
                if existing is not None:
                    await existing.aclose()
                self._cp_clients[name] = self._create_http_client(cp.url)
            self._cp_jwts[name] = cp.jwt_token

        if old.authz.url != new.authz.url:
            await self._replace_authz_client(new.authz.url)
        self._authz_jwt = new.authz.jwt_token

    async def aclose(self) -> None:
        """
        Close all managed clients.
        """
        clients: list[httpx.AsyncClient] = list(self._agent_clients.values())
        clients.extend(self._cp_clients.values())

        if self._authz_client is not None:
            clients.append(self._authz_client)

        for client in clients:
            await client.aclose()

    async def prune_agent_clients(self, active_targets: set[str]) -> None:
        """
        Close and remove agent clients whose targets are no longer active.
        """
        stale = [
            target for target in self._agent_clients if target not in active_targets
        ]
        for target in stale:
            client = self._agent_clients.pop(target)
            asyncio.create_task(client.aclose())

    async def proxy_to_agent(
        self,
        request: Request,
        route: RouteEntry,
        auth_headers: dict[str, str],
    ) -> Response:
        """
        Forward request to an agent.
        """
        upstream_path = request.url.path[len(route.prefix) :] or "/"
        client = self._get_agent_client(route)

        return await self._proxy_request(
            client=client,
            request=request,
            upstream_path=upstream_path,
            extra_headers=auth_headers,
        )

    async def proxy_to_controlplane(
        self,
        request: Request,
        route: RouteEntry,
        *,
        user_authorization: str | None = None,
    ) -> Response:
        """
        Forward request to the controlplane identified by the route.

        When user_authorization is provided (external TCP request whose JWT has
        already been validated), that token is forwarded as-is so the controlplane
        sees the original caller's identity.

        When it is None (local/agent-initiated request) the node attaches its own credentials instead.
        """
        cp_name = route.cp_name
        client = self._cp_clients.get(cp_name) if cp_name else None
        if client is None:
            log.warning("no controlplane client for route %r", route)
            return Response(status_code=503, content=b"controlplane not configured")

        if user_authorization is not None:
            auth_headers: dict[str, str] = {NODE_AUTH_HEADER: user_authorization}
        else:
            jwt = self._cp_jwts.get(cp_name, "")
            auth_headers = self._auth_headers(jwt)

        return await self._proxy_request(
            client=client,
            request=request,
            upstream_path=request.url.path,
            extra_headers=auth_headers,
        )

    async def proxy_to_authz(self, request: Request) -> Response:
        """
        Forward request to authz service.
        """
        if self._authz_client is None:
            return Response(status_code=503, content=b"authz not configured")

        return await self._proxy_request(
            client=self._authz_client,
            request=request,
            upstream_path=request.url.path,
            extra_headers=self._auth_headers(self._authz_jwt),
        )

    def _get_agent_client(self, route: RouteEntry) -> httpx.AsyncClient:
        """
        Return cached client for an agent endpoint.
        """
        client = self._agent_clients.get(route.target)
        if client:
            return client

        if route.transport == "uds":
            client = httpx.AsyncClient(
                transport=httpx.AsyncHTTPTransport(uds=route.target),
                base_url=AGENT_BASE_URL,
                timeout=DEFAULT_TIMEOUT,
            )
        else:
            client = self._create_http_client(f"http://{route.target}")

        self._agent_clients[route.target] = client
        return client

    def _create_http_client(self, base_url: str) -> httpx.AsyncClient:
        """
        Create a standard HTTP client.
        """
        return httpx.AsyncClient(
            base_url=base_url,
            timeout=DEFAULT_TIMEOUT,
        )

    async def _replace_authz_client(self, base_url: str | None) -> None:
        """
        Swap authz client after config change.
        """
        old = self._authz_client

        self._authz_client = self._create_http_client(base_url) if base_url else None

        if old is not None:
            await old.aclose()

    def _auth_headers(self, jwt: str) -> dict[str, str]:
        """
        Auth headers for node-to-node communication.
        """
        return {
            NODE_AUTH_HEADER: f"Token {jwt}",
        }

    async def _proxy_request(
        self,
        client: httpx.AsyncClient,
        request: Request,
        upstream_path: str,
        extra_headers: dict[str, str],
    ) -> Response:
        """
        Forward request to upstream service.
        """
        body = await self._read_limited_body(request)
        if body is None:
            return Response(status_code=413, content=b"request body too large")

        try:
            upstream_request = client.build_request(
                method=request.method,
                url=self._build_upstream_url(request, upstream_path),
                headers=self._build_upstream_headers(request, extra_headers),
                content=body,
            )

            upstream_response = await client.send(
                upstream_request,
                stream=True,
            )

        except httpx.ConnectError:
            return Response(status_code=502, content=b"bad gateway")

        except httpx.ReadTimeout:
            return Response(status_code=504, content=b"gateway timeout")

        except httpx.HTTPError as exc:
            log.warning("proxy error: %s", exc)
            return Response(status_code=502, content=b"bad gateway")

        return StreamingResponse(
            self._stream_response(upstream_response),
            status_code=upstream_response.status_code,
            headers=self._filter_response_headers(upstream_response),
        )

    @staticmethod
    async def _read_limited_body(request: Request) -> bytes | None:
        """
        Read request body up to MAX_BODY_SIZE; return None if the limit is exceeded.
        """
        cl = request.headers.get("content-length")
        if cl is not None:
            try:
                if int(cl) > MAX_BODY_SIZE:
                    return None
            except ValueError:
                pass

        chunks: list[bytes] = []
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > MAX_BODY_SIZE:
                return None
            chunks.append(chunk)
        return b"".join(chunks)

    def _build_upstream_url(self, request: Request, upstream_path: str) -> str:
        """
        Preserve query string when forwarding.
        """
        if request.url.query:
            return f"{upstream_path}?{request.url.query}"

        return upstream_path

    def _build_upstream_headers(
        self,
        request: Request,
        extra_headers: dict[str, str],
    ) -> dict[str, str]:
        """
        Remove unsafe headers and inject trusted headers.
        """
        headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() not in HOP_BY_HOP_HEADERS
            and key.lower() not in SENSITIVE_INBOUND_HEADERS
        }

        headers.update(extra_headers)
        return headers

    def _filter_response_headers(
        self,
        response: httpx.Response,
    ) -> dict[str, str]:
        """
        Remove hop-by-hop response headers.
        """
        return {
            key: value
            for key, value in response.headers.items()
            if key.lower() not in HOP_BY_HOP_HEADERS
        }

    @staticmethod
    async def _stream_response(
        response: httpx.Response,
    ) -> AsyncIterator[bytes]:
        """
        Stream response body safely.
        """
        try:
            async for chunk in response.aiter_bytes():
                yield chunk
        finally:
            await response.aclose()
