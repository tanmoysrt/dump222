from __future__ import annotations

import asyncio
import logging
import os
import signal
from pathlib import Path

import uvicorn

from press_node_agent.apps import create_internal_node_app, create_proxy_app
from press_node_agent.config import ConfigManager
from press_node_agent.discovery import AgentDiscovery
from press_node_agent.auth import JWKSManager
from press_node_agent.proxy_client import ProxyClient
from press_node_agent.routes import RouteRegistry

log = logging.getLogger("press_node_agent")


async def run() -> None:
    # Setup Logger
    log_level = os.environ.get("LOG_LEVEL", "INFO")
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    # Setup Config and Dependencies
    config_manager = ConfigManager()
    config = config_manager.current
    Path(config.socket_dir).mkdir(parents=True, exist_ok=True)
    Path(config.admin_socket).unlink(missing_ok=True)

    route_registry = RouteRegistry(config_manager)
    jwks_manager = JWKSManager(config_manager)
    proxy_client = ProxyClient(config_manager)
    agent_discovery = AgentDiscovery(
        config_manager=config_manager,
        route_registry=route_registry,
        proxy_client=proxy_client,
    )

    # Create uvicorn servers for proxy and admin part
    proxy_server = uvicorn.Server(
        uvicorn.Config(
            create_proxy_app(
                config_manager,
                jwks_manager,
                route_registry,
                proxy_client,
            ),
            host=config.listen.host,
            port=config.listen.port,
            log_level=log_level.lower(),
            access_log=False,
        )
    )
    local_node_server = uvicorn.Server(
        uvicorn.Config(
            create_internal_node_app(
                config_manager,
                agent_discovery,
                jwks_manager,
                route_registry,
                proxy_client,
            ),
            uds=config.admin_socket,
            log_level=log_level.lower(),
            access_log=False,
        )
    )

    # Do a initial scan of the socket directory
    # to discover agents before starting servers
    await agent_discovery.reconcile()

    # Setup signal handlers for graceful shutdown
    stop_event = asyncio.Event()

    asyncio_loop = asyncio.get_running_loop()
    for s in (signal.SIGTERM, signal.SIGINT):
        try:
            asyncio_loop.add_signal_handler(s, stop_event.set)
        except NotImplementedError:
            pass

    # Run servers and background tasks in asyncio
    asyncio_tasks = [
        asyncio.create_task(proxy_server.serve(), name="proxy_server"),
        asyncio.create_task(local_node_server.serve(), name="local_node_server"),
        asyncio.create_task(
            jwks_manager.refresh_keys_in_background(), name="jwks_key_refresh"
        ),
        asyncio.create_task(agent_discovery.start(), name="discovery"),
        asyncio.create_task(config_manager.watch(), name="config_watch"),
    ]

    # Graceful shutdown on signal
    await stop_event.wait()
    log.info("shutting down")

    proxy_server.should_exit = True
    local_node_server.should_exit = True
    jwks_manager.stop()
    agent_discovery.stop()

    for t in asyncio_tasks:
        try:
            await asyncio.wait_for(t, timeout=10.0)
        except asyncio.TimeoutError, asyncio.CancelledError:
            t.cancel()
        except Exception:
            log.exception("task error during shutdown")

    await proxy_client.aclose()

    # Cleanup admin socket on exit
    Path(config.admin_socket).unlink(missing_ok=True)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
