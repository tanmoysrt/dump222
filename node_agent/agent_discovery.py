from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx
from watchfiles import Change, awatch

from .routes import RouteRegistry

log = logging.getLogger(__name__)

RECONCILE_INTERVAL = 300


@dataclass
class AgentTarget:
    transport: Literal["uds", "http"]
    target: str


class AgentDiscovery:
    def __init__(
        self,
        socket_dir: str,
        admin_socket: str,
        allow_http: bool,
        registry: RouteRegistry,
    ) -> None:
        self._dir = Path(socket_dir)
        self._admin_sock_name = Path(admin_socket).name
        self._allow_http = allow_http
        self._registry = registry
        self._known: dict[str, AgentTarget] = {}
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()

    def update_allow_http(self, allow: bool) -> None:
        self._allow_http = allow

    async def initial_scan(self) -> None:
        await self._reconcile()

    async def run(self) -> None:
        watcher = asyncio.create_task(self._watch())
        recon = asyncio.create_task(self._reconcile_loop())
        try:
            await self._stop.wait()
        finally:
            watcher.cancel()
            recon.cancel()
            for t in (watcher, recon):
                try:
                    await t
                except asyncio.CancelledError:
                    pass

    def stop(self) -> None:
        self._stop.set()

    async def refresh_agent(self, agent: str) -> bool:
        async with self._lock:
            entry = self._known.get(agent)
            if entry is None:
                return False
            await self._registry.deregister_agent(agent)
        await self._register_agent(agent, entry)
        return True

    async def _watch(self) -> None:
        while not self._stop.is_set():
            try:
                async for changes in awatch(str(self._dir), stop_event=self._stop):
                    for change, path_str in changes:
                        p = Path(path_str)
                        if not self._is_agent_file(p):
                            continue
                        if change in (Change.added, Change.modified):
                            await self._handle_file_added(p)
                        elif change == Change.deleted:
                            await self._handle_file_removed(p)
            except Exception:
                log.exception("inotify watcher crashed, restarting in 5s")
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=5.0)
                    return
                except asyncio.TimeoutError:
                    continue

    async def _reconcile_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=RECONCILE_INTERVAL)
                return
            except asyncio.TimeoutError:
                pass
            try:
                await self._reconcile()
            except Exception:
                log.exception("reconcile failed")

    async def _reconcile(self) -> None:
        present = self._scan_dir()
        async with self._lock:
            current_agents = set(self._known.keys())
            current_snapshot = dict(self._known)

        stale = current_agents - present.keys()
        for agent in stale:
            async with self._lock:
                self._known.pop(agent, None)
            await self._registry.deregister_agent(agent)
            log.info("agent %s removed (reconcile)", agent)

        for agent, target in present.items():
            if agent not in current_agents or current_snapshot.get(agent) != target:
                if agent in current_agents:
                    async with self._lock:
                        self._known.pop(agent, None)
                    await self._registry.deregister_agent(agent)
                await self._register_agent(agent, target)

    def _is_agent_file(self, p: Path) -> bool:
        return p.name != self._admin_sock_name and p.suffix in (".sock", ".http")

    def _scan_dir(self) -> dict[str, AgentTarget]:
        if not self._dir.exists():
            return {}

        out: dict[str, AgentTarget] = {}
        http_files: list[Path] = []

        for entry in self._dir.iterdir():
            if not self._is_agent_file(entry):
                continue
            if entry.suffix == ".sock":
                out[entry.stem] = AgentTarget("uds", str(entry))
            elif entry.suffix == ".http":
                http_files.append(entry)

        for path in http_files:
            if path.stem in out:
                continue
            if not self._allow_http:
                log.error(
                    "rejecting HTTP agent %s: allow_http_agents disabled", path.stem
                )
                continue
            addr = self._read_http_target(path)
            if addr:
                out[path.stem] = AgentTarget("http", addr)

        return out

    def _read_http_target(self, path: Path) -> str | None:
        try:
            addr = path.read_text().strip().splitlines()[0].strip()
        except Exception:
            log.exception("failed reading %s", path)
            return None
        if not addr:
            log.error("empty .http file for %s", path.stem)
            return None
        return addr

    async def _handle_file_added(self, p: Path) -> None:
        if p.suffix == ".sock":
            await self._handle_sock_added(p)
        elif p.suffix == ".http":
            await self._handle_http_added(p)

    async def _handle_sock_added(self, p: Path) -> None:
        agent = p.stem
        target = AgentTarget("uds", str(p))

        async with self._lock:
            existing = self._known.get(agent)
            if existing and existing == target:
                return
            if existing and existing.transport == "http":
                await self._registry.deregister_agent(agent)
                self._known.pop(agent, None)

        await self._register_agent(agent, target)

    async def _handle_http_added(self, p: Path) -> None:
        agent = p.stem

        if not self._allow_http:
            log.error("rejecting HTTP agent %s: allow_http_agents disabled", agent)
            return

        if (self._dir / f"{agent}.sock").exists():
            return

        addr = self._read_http_target(p)
        if not addr:
            return

        target = AgentTarget("http", addr)
        async with self._lock:
            if agent in self._known and self._known[agent] == target:
                return

        await self._register_agent(agent, target)

    async def _handle_file_removed(self, p: Path) -> None:
        agent = p.stem
        async with self._lock:
            entry = self._known.get(agent)
        if entry is None:
            return

        if p.suffix == ".sock" and entry.transport == "uds":
            await self._registry.deregister_agent(agent)
            async with self._lock:
                self._known.pop(agent, None)
            log.info("agent %s deregistered (.sock removed)", agent)
            http_path = self._dir / f"{agent}.http"
            if http_path.exists():
                await self._handle_http_added(http_path)

        elif p.suffix == ".http" and entry.transport == "http":
            await self._registry.deregister_agent(agent)
            async with self._lock:
                self._known.pop(agent, None)
            log.info("agent %s deregistered (.http removed)", agent)

    async def _register_agent(self, agent: str, target: AgentTarget) -> None:
        try:
            entries = await fetch_agent_routes(target.transport, target.target)
        except Exception as e:
            log.error("failed to fetch metadata from %s: %s", agent, e)
            return

        registered, rejected = await self._registry.register_agent_routes(
            agent,
            target.transport,
            target.target,
            entries,
        )
        async with self._lock:
            self._known[agent] = target
        log.info("agent %s registered: %s rejected=%s", agent, registered, rejected)

    async def on_allow_http_changed(self, allow: bool) -> None:
        self._allow_http = allow
        if not allow:
            async with self._lock:
                to_remove = [a for a, e in self._known.items() if e.transport == "http"]
                for agent in to_remove:
                    self._known.pop(agent, None)
            for agent in to_remove:
                await self._registry.deregister_agent(agent)
                log.info("agent %s deregistered (allow_http_agents disabled)", agent)
        else:
            await self._reconcile()

    async def refresh_all(self) -> None:
        async with self._lock:
            agents = list(self._known.items())
        for agent, target in agents:
            await self._registry.deregister_agent(agent)
            await self._register_agent(agent, target)


async def fetch_agent_routes(
    transport: Literal["uds", "http"], target: str
) -> list[dict]:
    if transport == "uds":
        t = httpx.AsyncHTTPTransport(uds=target)
        client = httpx.AsyncClient(transport=t, base_url="http://agent", timeout=5.0)
    else:
        client = httpx.AsyncClient(base_url=f"http://{target}", timeout=5.0)
    async with client as c:
        r = await c.get("/_meta/routes")
        r.raise_for_status()
        try:
            data = r.json()
        except Exception:
            log.error("invalid JSON from %s/_meta/routes", target)
            return []
        return list(data.get("routes", []))
