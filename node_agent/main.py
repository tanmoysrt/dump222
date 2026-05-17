from __future__ import annotations

import asyncio
import logging
import os
import signal
from pathlib import Path

import uvicorn

from .admin_app import init_admin_app
from .proxy_app import apply_config, init_proxy_app
from .config import Config, ConfigHolder, load_config
from .agent_discovery import AgentDiscovery
from .auth import JWKSManager
from .proxy_client import ProxyClient
from .routes import RouteEntry, RouteRegistry

log = logging.getLogger("node_agent")


async def amain() -> None:
    log_level = os.environ.get("LOG_LEVEL", "INFO")
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    cfg = load_config()
    holder = ConfigHolder(cfg)

    registry = RouteRegistry()
    jwks = JWKSManager(cfg.jwks.url, cfg.jwks.cache_ttl_seconds)
    proxy = ProxyClient(cfg.node_jwt)
    await apply_config(cfg, jwks, registry, proxy)

    discovery = AgentDiscovery(
        socket_dir=cfg.socket_dir,
        admin_socket=cfg.admin_socket,
        allow_http=cfg.allow_http_agents,
        registry=registry,
    )

    async def on_reload(old: Config, new: Config) -> None:
        jwks.update_config(new.jwks.url, new.jwks.cache_ttl_seconds)
        proxy.set_controlplane(new.controlplane.url)
        proxy.set_authz(new.authz.url if new.authz is not None else None)
        proxy.set_node_jwt(new.node_jwt)

        new_routes = [
            r
            for r in registry.snapshot()
            if r.dest_type not in ("controlplane", "authz")
            and (new.allow_http_agents or r.transport != "http")
        ]
        for p in new.controlplane.prefixes:
            new_routes.append(
                RouteEntry(
                    prefix=p,
                    dest_type="controlplane",
                    target=new.controlplane.url,
                )
            )
        if new.authz is not None:
            for p in new.authz.prefixes:
                new_routes.append(
                    RouteEntry(
                        prefix=p,
                        dest_type="authz",
                        target=new.authz.url,
                    )
                )
        await registry.replace_atomic(new_routes)

        if old.allow_http_agents != new.allow_http_agents:
            await discovery.on_allow_http_changed(new.allow_http_agents)
        await discovery.refresh_all()

    holder.subscribe(on_reload)

    proxy_app = init_proxy_app(holder, jwks, registry, proxy)
    admin_app = init_admin_app(holder, discovery, jwks, registry, proxy)

    Path(cfg.socket_dir).mkdir(parents=True, exist_ok=True)
    Path(cfg.admin_socket).unlink(missing_ok=True)

    proxy_server = uvicorn.Server(
        uvicorn.Config(
            proxy_app,
            host=cfg.listen.host,
            port=cfg.listen.port,
            log_level=log_level.lower(),
            access_log=False,
        )
    )
    admin_server = uvicorn.Server(
        uvicorn.Config(
            admin_app,
            uds=cfg.admin_socket,
            log_level=log_level.lower(),
            access_log=False,
        )
    )

    await discovery.initial_scan()

    stop_evt = asyncio.Event()

    def _handle_shutdown() -> None:
        stop_evt.set()

    loop = asyncio.get_running_loop()
    for s in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(s, _handle_shutdown)
        except NotImplementedError:
            pass

    tasks = [
        asyncio.create_task(proxy_server.serve(), name="proxy_server"),
        asyncio.create_task(admin_server.serve(), name="admin_server"),
        asyncio.create_task(jwks.run_background(), name="jwks_bg"),
        asyncio.create_task(discovery.run(), name="discovery"),
        asyncio.create_task(holder.watch(), name="config_watch"),
    ]

    await stop_evt.wait()
    log.info("shutting down")

    proxy_server.should_exit = True
    admin_server.should_exit = True
    jwks.stop()
    discovery.stop()

    for t in tasks:
        try:
            await asyncio.wait_for(t, timeout=10.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            t.cancel()
        except Exception:
            log.exception("task error during shutdown")

    await proxy.aclose()
    Path(cfg.admin_socket).unlink(missing_ok=True)


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
