from __future__ import annotations

import logging
from typing import Any, AsyncIterator

import httpx
from fastapi import Request, Response
from fastapi.responses import StreamingResponse

from .routes import RouteEntry

log = logging.getLogger(__name__)

HOP_BY_HOP = {
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

# headers an upstream agent must never be able to spoof; we strip from inbound
SENSITIVE_INBOUND = {
    "x-auth-source",
    "x-auth-sub",
    "x-auth-roles",
    "x-auth-jti",
    "authorization",
}


class ProxyClient:
    def __init__(self, node_jwt: str) -> None:
        self._agent_clients: dict[str, httpx.AsyncClient] = {}
        self._cp_client: httpx.AsyncClient | None = None
        self._cp_url: str | None = None
        self._authz_client: httpx.AsyncClient | None = None
        self._authz_url: str | None = None
        self._stale_clients: list[httpx.AsyncClient] = []
        self._node_jwt = node_jwt

    def set_controlplane(self, url: str) -> None:
        if self._cp_url == url and self._cp_client is not None:
            return
        old = self._cp_client
        self._cp_url = url
        self._cp_client = httpx.AsyncClient(base_url=url, timeout=30.0)
        if old is not None:
            self._stale_clients.append(old)

    def set_authz(self, url: str | None) -> None:
        if self._authz_url == url and (url is None or self._authz_client is not None):
            return
        old = self._authz_client
        self._authz_url = url
        self._authz_client = (
            httpx.AsyncClient(base_url=url, timeout=30.0) if url else None
        )
        if old is not None:
            self._stale_clients.append(old)

    def set_node_jwt(self, token: str) -> None:
        self._node_jwt = token

    def _agent_client(self, route: RouteEntry) -> httpx.AsyncClient:
        c = self._agent_clients.get(route.target)
        if c is None:
            if route.transport == "uds":
                t = httpx.AsyncHTTPTransport(uds=route.target)
                c = httpx.AsyncClient(
                    transport=t, base_url="http://agent", timeout=30.0
                )
            else:
                c = httpx.AsyncClient(base_url=f"http://{route.target}", timeout=30.0)
            self._agent_clients[route.target] = c
        return c

    async def aclose(self) -> None:
        for c in list(self._agent_clients.values()):
            await c.aclose()
        if self._cp_client is not None:
            await self._cp_client.aclose()
        if self._authz_client is not None:
            await self._authz_client.aclose()
        for c in self._stale_clients:
            await c.aclose()
        self._stale_clients.clear()

    async def proxy_to_agent(
        self, request: Request, route: RouteEntry, auth_headers: dict[str, str]
    ) -> Response:
        stripped = request.url.path[len(route.prefix) :] or "/"
        client = self._agent_client(route)
        return await self._forward(client, request, stripped, auth_headers)

    async def proxy_to_controlplane(
        self, request: Request, route: RouteEntry
    ) -> Response:
        assert self._cp_client is not None
        headers = {"authorization": f"Token {self._node_jwt}"}
        return await self._forward(self._cp_client, request, request.url.path, headers)

    async def proxy_to_authz(self, request: Request, route: RouteEntry) -> Response:
        if self._authz_client is None:
            return Response(status_code=503, content=b"authz not configured")
        headers = {"authorization": f"Token {self._node_jwt}"}
        return await self._forward(
            self._authz_client, request, request.url.path, headers
        )

    async def _forward(
        self,
        client: httpx.AsyncClient,
        request: Request,
        upstream_path: str,
        extra_headers: dict[str, str],
    ) -> Response:
        hdrs: dict[str, str] = {}
        for k, v in request.headers.items():
            lk = k.lower()
            if lk in HOP_BY_HOP or lk in SENSITIVE_INBOUND:
                continue
            hdrs[k] = v
        hdrs.update(extra_headers)
        url = upstream_path
        if request.url.query:
            url = f"{url}?{request.url.query}"
        try:
            body = await request.body()
            upstream_req = client.build_request(
                request.method,
                url,
                content=body,
                headers=hdrs,
            )
            resp = await client.send(upstream_req, stream=True)
        except httpx.ConnectError:
            return Response(status_code=502, content=b"bad gateway")
        except httpx.ReadTimeout:
            return Response(status_code=504, content=b"gateway timeout")
        except httpx.HTTPError as e:
            log.warning("proxy error: %s", e)
            return Response(status_code=502, content=b"bad gateway")
        out_headers = {
            k: v for k, v in resp.headers.items() if k.lower() not in HOP_BY_HOP
        }

        async def _body() -> AsyncIterator[bytes]:
            try:
                async for chunk in resp.aiter_bytes():
                    yield chunk
            finally:
                await resp.aclose()

        return StreamingResponse(
            _body(), status_code=resp.status_code, headers=out_headers
        )
