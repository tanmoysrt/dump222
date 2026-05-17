"""Dummy agent template. Single-file example showing how to write a Press agent.

Two transports are supported:

  default          -> UDS at /run/press-node-agent/dummy.sock (production-style)
  USE_HTTP=1       -> HTTP at 127.0.0.1:8090, drops /run/press-node-agent/dummy.http
                      (dev-only; requires `allow_http_agents: true` in node-agent config)

The agent MUST expose `GET /_meta/routes` describing every prefix it owns and any
public whitelist entries. Node Agent strips the prefix before forwarding, so each
handler sees the path relative to its prefix (e.g. `/api/list`, not `/dummy/api/list`).

Inbound requests carry headers stamped by Node Agent:

  X-Auth-Source: external | local
  X-Auth-Sub:    <user id>      (external only)
  X-Auth-Roles:  comma list     (external only)
  X-Auth-Jti:    <token id>     (external only)

If X-Auth-Source is missing, the request bypassed Node Agent -> deny.
If X-Auth-Source: local, the call is from another agent -> trust, skip authz.
If X-Auth-Source: external and role is `user`, call Node Agent's admin socket
(/_admin/check-permission over /run/press-node-agent/node.sock) before acting on a
resource. `admin` and `telemetry` roles are agent-decided shortcuts.

Run:
    python development/dummy_agent.py
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request

logging.basicConfig(level=logging.INFO, format="%(asctime)s dummy %(message)s")
log = logging.getLogger("dummy-agent")

SOCKET_DIR = Path(os.environ.get("SOCKET_DIR", "/run/press-node-agent"))
ADMIN_SOCK = SOCKET_DIR / "node.sock"
AGENT_NAME = "dummy"

app = FastAPI()


@app.get("/_meta/routes")
async def meta_routes() -> dict:
    return {
        "routes": [
            {
                "prefix": "/dummy",
                "whitelist": [
                    {"path": "/dummy/health"},
                    {"path": "/dummy/stream/", "match": "prefix"},
                ],
            },
        ],
    }


@app.get("/health")
async def health() -> dict:
    # whitelisted; Node Agent does not stamp auth on this path
    return {"status": "ok"}


def _check_auth(request: Request) -> tuple[str, str, str, str]:
    src = request.headers.get("x-auth-source")
    if not src:
        raise HTTPException(status_code=401, detail="missing X-Auth-Source")
    sub = request.headers.get("x-auth-sub", "")
    roles = request.headers.get("x-auth-roles", "")
    jti = request.headers.get("x-auth-jti", "")
    return src, sub, roles, jti


async def _ask_node_agent(
    sub: str, jti: str, resource_type: str, resource_id: str, action: str
) -> bool:
    transport = httpx.AsyncHTTPTransport(uds=str(ADMIN_SOCK))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://node", timeout=5.0
    ) as c:
        r = await c.post(
            "/_admin/check-permission",
            json={
                "sub": sub,
                "jti": jti,
                "resource": {"type": resource_type, "id": resource_id},
                "action": action,
            },
        )
    if r.status_code == 503:
        raise HTTPException(status_code=503, detail="authz unavailable")
    r.raise_for_status()
    return bool(r.json().get("allowed"))


@app.get("/things/{thing_id}")
async def get_thing(thing_id: str, request: Request) -> dict:
    src, sub, roles, jti = _check_auth(request)
    role_list = [r for r in roles.split(",") if r]
    if src == "local":
        pass
    elif "admin" in role_list or "telemetry" in role_list:
        pass  # role-based shortcut, no authz server call
    else:
        allowed = await _ask_node_agent(sub, jti, "Thing", thing_id, "Read")
        if not allowed:
            raise HTTPException(status_code=403, detail="forbidden")
    return {"id": thing_id, "by": sub or "<local>"}


def _serve_uds() -> None:
    SOCKET_DIR.mkdir(parents=True, exist_ok=True)
    sock = SOCKET_DIR / f"{AGENT_NAME}.sock"
    if sock.exists():
        sock.unlink()
    log.info("listening on %s", sock)
    uvicorn.run(app, uds=str(sock), log_level="warning")


def _serve_http() -> None:
    host = os.environ.get("HTTP_HOST", "127.0.0.1")
    port = int(os.environ.get("HTTP_PORT", "8090"))
    SOCKET_DIR.mkdir(parents=True, exist_ok=True)
    marker = SOCKET_DIR / f"{AGENT_NAME}.http"
    marker.write_text(f"{host}:{port}\n")
    log.info("listening on %s:%s, marker at %s", host, port, marker)
    try:
        uvicorn.run(app, host=host, port=port, log_level="warning")
    finally:
        try:
            marker.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    if os.environ.get("USE_HTTP"):
        _serve_http()
    else:
        _serve_uds()
