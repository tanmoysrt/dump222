from __future__ import annotations

import httpx
from press_api_spec.node_agent_service_v1.endpoints import HasPermission, RefreshAgent
from press_api_spec.node_agent_service_v1.models import (
    CheckRequest,
    CheckResource,
    HasPermissionResponse,
    RefreshAgentResponse,
)

from .config import get_node_agent_socket


def send_request(
    method: str,
    path: str,
    *,
    json: dict | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
    node_socket: str | None = None,
) -> httpx.Response:
    """Send a request through the node-agent local proxy (sync).

    Use this to reach controlplanes or other agents without managing
    credentials. Node-agent attaches the correct JWT downstream for
    controlplane calls, and stamps X-Auth-Source: local for
    agent-to-agent calls.
    """
    sock = node_socket or get_node_agent_socket()
    transport = httpx.HTTPTransport(uds=sock)

    with httpx.Client(
        transport=transport,
        base_url="http://node",
        timeout=timeout,
    ) as client:
        return client.request(method, path, json=json, headers=headers)


def has_permission(
    sub: str,
    jti: str,
    resource_type: str,
    resource_id: str,
    action: str,
    *,
    timeout: float = 5.0,
    node_socket: str | None = None,
) -> bool:
    """Check if a user is allowed to perform an action on a resource (sync).

    Calls /_admin/has-permission on the node-agent admin socket,
    which consults the remote authz service with a local cache.

    Raises:
        httpx.HTTPStatusError: If the authz service is unreachable (503).
    """
    sock = node_socket or get_node_agent_socket()
    transport = httpx.HTTPTransport(uds=sock)

    with httpx.Client(
        transport=transport,
        base_url="http://node",
        timeout=timeout,
    ) as client:
        req = CheckRequest(
            sub=sub,
            jti=jti,
            resource=CheckResource(type=resource_type, id=resource_id),
            action=action,
        )
        resp = client.post(
            HasPermission.full_path,
            json=req.model_dump(),
        )

    if resp.status_code == 503:
        raise httpx.HTTPStatusError(
            "authz service unavailable", request=resp.request, response=resp
        )
    resp.raise_for_status()
    return HasPermissionResponse.model_validate(resp.json()).allowed


def refresh_agent_routes(
    agent: str | None = None,
    *,
    timeout: float = 5.0,
    node_socket: str | None = None,
) -> RefreshAgentResponse:
    """Trigger a route refresh for an agent or all agents (sync)."""
    sock = node_socket or get_node_agent_socket()
    transport = httpx.HTTPTransport(uds=sock)

    params = {"agent": agent} if agent else {}

    with httpx.Client(
        transport=transport,
        base_url="http://node",
        timeout=timeout,
    ) as client:
        resp = client.post(RefreshAgent.full_path, params=params)

    resp.raise_for_status()
    return RefreshAgentResponse.model_validate(resp.json())
    return resp.json()
