"""Dummy agent template. Single-file example showing how to write a Press agent.

Two transports are supported:

  default          -> UDS at /run/press-node-agent/dummy.sock (production-style)
  USE_HTTP=1       -> HTTP at 127.0.0.1:8090, drops /run/press-node-agent/dummy.http
                      (dev-only; requires `allow_http_agents: true` in node-agent config)

Check docs/3-write-agent.md for writing local agents.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from press_node_agent_client import (
    AuthContext,
    get_my_agent_socket_path,
    has_permission_async,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s dummy %(message)s")
log = logging.getLogger("dummy-agent")

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


@app.get("/dummy/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/dummy/things/{thing_id}")
async def get_thing(thing_id: str, request: Request) -> dict:
    auth = AuthContext.from_headers(request.headers)
    if not auth.source:
        raise HTTPException(status_code=401, detail="missing X-Auth-Source")

    if auth.is_local:
        pass
    elif auth.has_role("admin", "telemetry"):
        pass
    else:
        allowed = await has_permission_async(
            sub=auth.sub,
            jti=auth.jti,
            resource_type="Thing",
            resource_id=thing_id,
            action="Read",
        )
        if not allowed:
            raise HTTPException(status_code=403, detail="forbidden")
    return {"id": thing_id, "by": auth.sub or "<local>"}


def _serve_uds() -> None:
    sock_path = get_my_agent_socket_path(AGENT_NAME)
    Path(sock_path).parent.mkdir(parents=True, exist_ok=True)
    sock = Path(sock_path)
    if sock.exists():
        sock.unlink()
    log.info("listening on %s", sock)
    uvicorn.run(app, uds=str(sock), log_level="warning")


def _serve_http() -> None:
    host = os.environ.get("HTTP_HOST", "127.0.0.1")
    port = int(os.environ.get("HTTP_PORT", "8090"))
    socket_dir = Path(get_my_agent_socket_path(AGENT_NAME)).parent
    socket_dir.mkdir(parents=True, exist_ok=True)
    marker = socket_dir / f"{AGENT_NAME}.http"
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
