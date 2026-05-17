from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx
from watchfiles import awatch
from press_api_spec.node_agent_contract_v1.endpoints import GetRoutes
from press_api_spec.node_agent_contract_v1.models import (
    RouteDeclaration,
    RoutesResponse,
)

from .config import Config, ConfigManager
from .proxy_client import ProxyClient
from .routes import RouteRegistry

log = logging.getLogger(__name__)

RECONCILE_INTERVAL_SECONDS = 300


@dataclass(frozen=True)
class AgentEndpoint:
    """
    Where an agent can be reached.

    transport:
        - "uds"  -> Unix domain socket
        - "http" -> HTTP host:port
    """

    transport: Literal["uds", "http"]
    address: str


class AgentDiscovery:
    """
    Keeps RouteRegistry in sync with agents discovered from the socket directory.

    Discovery rules:
    - *.sock files are preferred
    - *.http files are only used when HTTP agents are enabled
    - if both exist for the same agent, .sock wins
    """

    def __init__(
        self,
        config_manager: ConfigManager,
        route_registry: RouteRegistry,
        proxy_client: ProxyClient,
    ) -> None:
        cfg = config_manager.current

        self._socket_dir = Path(cfg.socket_dir)
        self._admin_socket_name = Path(cfg.admin_socket).name
        self._http_agents_enabled = cfg.allow_http_agents
        self._route_registry = route_registry
        self._proxy_client = proxy_client

        # Current registered agents
        self._agents: dict[str, AgentEndpoint] = {}

        self._lock = asyncio.Lock()
        self._reconcile_lock = asyncio.Lock()
        self._shutdown = asyncio.Event()

        config_manager.subscribe(self._on_config_changed)

    async def start(self) -> None:
        """
        Start background discovery tasks.

        This method blocks until stop() is called.
        """
        await self.reconcile()

        watcher_task = asyncio.create_task(self._watch_filesystem())
        reconcile_task = asyncio.create_task(self._reconcile_forever())

        try:
            await self._shutdown.wait()
        finally:
            watcher_task.cancel()
            reconcile_task.cancel()
            await asyncio.gather(
                watcher_task,
                reconcile_task,
                return_exceptions=True,
            )

    def stop(self) -> None:
        """Stop background tasks."""
        self._shutdown.set()

    async def refresh_agent(self, agent_name: str) -> bool:
        """
        Re-fetch routes for one already-known agent.
        """
        async with self._lock:
            endpoint = self._agents.get(agent_name)

        if endpoint is None:
            return False

        await self._register_agent(agent_name, endpoint)
        return True

    async def refresh_all(self) -> None:
        """Re-fetch routes for all currently registered agents."""
        async with self._lock:
            agents = dict(self._agents)

        for agent_name, endpoint in agents.items():
            await self._register_agent(agent_name, endpoint)

    async def _on_config_changed(self, old: Config, new: Config) -> None:
        """Config callback from ConfigManager."""
        if old.allow_http_agents != new.allow_http_agents:
            self._http_agents_enabled = new.allow_http_agents

        await self.reconcile()

    async def reconcile(self) -> None:
        """
        Make actual registered agents match desired filesystem state.
        """
        async with self._reconcile_lock:
            desired = self._discover_agents()

            async with self._lock:
                current = dict(self._agents)

            current_names = set(current)
            desired_names = set(desired)

            agents_to_remove = current_names - desired_names
            agents_to_add_or_update = {
                name: endpoint
                for name, endpoint in desired.items()
                if current.get(name) != endpoint
            }

            for agent_name in agents_to_remove:
                await self._remove_agent(agent_name)

            for agent_name, endpoint in agents_to_add_or_update.items():
                await self._register_agent(agent_name, endpoint)

            active_targets = {ep.address for ep in desired.values()}
            await self._proxy_client.prune_agent_clients(active_targets)

    def _discover_agents(self) -> dict[str, AgentEndpoint]:
        """
        Read filesystem and determine desired agent state.
        """
        if not self._socket_dir.exists():
            return {}

        agents: dict[str, AgentEndpoint] = {}
        http_candidates: list[Path] = []

        for path in self._socket_dir.iterdir():
            if not self._is_agent_file(path):
                continue

            if path.suffix == ".sock":
                agents[path.stem] = AgentEndpoint(
                    transport="uds",
                    address=str(path),
                )

            elif path.suffix == ".http":
                http_candidates.append(path)

        if not self._http_agents_enabled:
            return agents

        for path in http_candidates:
            if path.stem in agents:
                continue

            address = self._read_http_address(path)
            if not address:
                continue

            agents[path.stem] = AgentEndpoint(
                transport="http",
                address=address,
            )

        return agents

    def _is_agent_file(self, path: Path) -> bool:
        """
        True if this file can represent an agent.
        """
        return path.name != self._admin_socket_name and path.suffix in {
            ".sock",
            ".http",
        }

    def _read_http_address(self, path: Path) -> str | None:
        """
        Read host:port from a .http file.
        """
        try:
            lines = path.read_text().splitlines()
        except Exception:
            log.exception("failed reading %s", path)
            return None

        if not lines:
            log.error("empty .http file: %s", path)
            return None

        address = lines[0].strip()

        if not address:
            log.error("empty .http file: %s", path)
            return None

        return address

    async def _register_agent(
        self,
        agent_name: str,
        endpoint: AgentEndpoint,
    ) -> None:
        """
        Fetch routes and register/update the agent.
        """
        try:
            routes = await self._fetch_routes(endpoint)
        except Exception:
            log.exception("failed to fetch routes from %s", agent_name)
            return

        registered, rejected = await self._route_registry.replace_agent_routes(
            agent_name,
            endpoint.transport,
            endpoint.address,
            routes,
        )

        async with self._lock:
            self._agents[agent_name] = endpoint

        log.info(
            "agent %s registered (%s routes, %s rejected)",
            agent_name,
            len(registered),
            len(rejected),
        )

    async def _remove_agent(self, agent_name: str) -> None:
        """
        Remove agent from registry and local state.
        """
        await self._route_registry.deregister_agent(agent_name)

        async with self._lock:
            self._agents.pop(agent_name, None)

        log.info("agent %s removed", agent_name)

    async def _watch_filesystem(self) -> None:
        """
        Watch filesystem changes and trigger reconciliation.
        """
        while not self._shutdown.is_set():
            try:
                async for _ in awatch(
                    str(self._socket_dir),
                    stop_event=self._shutdown,
                ):
                    await self.reconcile()

            except Exception:
                log.exception("filesystem watcher crashed; restarting in 5s")

                try:
                    await asyncio.wait_for(self._shutdown.wait(), timeout=5)
                    return
                except asyncio.TimeoutError:
                    pass

    async def _reconcile_forever(self) -> None:
        """
        Periodic safety reconciliation in case filesystem events are missed.
        """
        while not self._shutdown.is_set():
            try:
                await asyncio.wait_for(
                    self._shutdown.wait(),
                    timeout=RECONCILE_INTERVAL_SECONDS,
                )
                return
            except asyncio.TimeoutError:
                await self.reconcile()

    async def _fetch_routes(
        self,
        endpoint: AgentEndpoint,
    ) -> list[RouteDeclaration]:
        """
        Fetch route metadata from an agent.
        """
        async with self._create_client(endpoint) as client:
            response = await client.get(GetRoutes.full_path)
            response.raise_for_status()

            return RoutesResponse.model_validate(response.json()).routes

    def _create_client(self, endpoint: AgentEndpoint) -> httpx.AsyncClient:
        """
        Create HTTP client for the endpoint.
        """
        if endpoint.transport == "uds":
            transport = httpx.AsyncHTTPTransport(uds=endpoint.address)

            return httpx.AsyncClient(
                transport=transport,
                base_url="http://agent",
                timeout=5.0,
            )

        return httpx.AsyncClient(
            base_url=f"http://{endpoint.address}",
            timeout=5.0,
        )
