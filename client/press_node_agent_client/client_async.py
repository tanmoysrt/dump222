from __future__ import annotations

from typing import Any

import httpx

from .config import get_node_agent_socket


async def send_request(
    method: str,
    path: str,
    *,
    json: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
    node_socket: str | None = None,
) -> httpx.Response:
    """Send a request through the node-agent local proxy (async).

    Use this to reach controlplanes or other agents without managing
    credentials. Node-agent attaches the correct JWT downstream for
    controlplane calls, and stamps X-Auth-Source: local for
    agent-to-agent calls.
    """
    sock = node_socket or get_node_agent_socket()
    transport = httpx.AsyncHTTPTransport(uds=sock)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://node",
        timeout=timeout,
    ) as client:
        return await client.request(method, path, json=json, headers=headers)


async def has_permission(
    sub: str,
    jti: str,
    resource_type: str,
    resource_id: str,
    action: str,
    *,
    timeout: float = 5.0,
    node_socket: str | None = None,
) -> bool:
    """Check if a user is allowed to perform an action on a resource (async).

    Calls /_admin/has-permission on the node-agent admin socket,
    which consults the remote authz service with a local cache.

    Raises:
        httpx.HTTPStatusError: If the authz service is unreachable (503).
    """
    sock = node_socket or get_node_agent_socket()
    transport = httpx.AsyncHTTPTransport(uds=sock)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://node",
        timeout=timeout,
    ) as client:
        resp = await client.post(
            "/_admin/has-permission",
            json={
                "sub": sub,
                "jti": jti,
                "resource": {"type": resource_type, "id": resource_id},
                "action": action,
            },
        )

    if resp.status_code == 503:
        raise httpx.HTTPStatusError(
            "authz service unavailable", request=resp.request, response=resp
        )
    resp.raise_for_status()
    return bool(resp.json().get("allowed"))


async def refresh_agent_routes(
    agent: str | None = None,
    *,
    timeout: float = 5.0,
    node_socket: str | None = None,
) -> dict[str, Any]:
    """Trigger a route refresh for an agent or all agents (async)."""
    sock = node_socket or get_node_agent_socket()
    transport = httpx.AsyncHTTPTransport(uds=sock)

    params = {"agent": agent} if agent else {}

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://node",
        timeout=timeout,
    ) as client:
        resp = await client.post("/_admin/refresh-agent", params=params)

    resp.raise_for_status()
    return resp.json()
