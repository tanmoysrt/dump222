from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Literal

from .config import Config, ConfigManager

log = logging.getLogger(__name__)

MatchMode = Literal["exact", "exact_or_prefix", "prefix"]


@dataclass
class WhitelistEntry:
    path: str
    match: MatchMode = "exact"

    def matches(self, path: str) -> bool:
        """Check if the given path matches this whitelist entry."""
        if self.match == "exact":
            return path == self.path
        if self.match == "exact_or_prefix":
            return path == self.path or path.startswith(self.path.rstrip("/") + "/")
        if self.match == "prefix":
            base = self.path if self.path.endswith("/") else self.path + "/"
            return path.startswith(base) and path != self.path
        return False


@dataclass
class RouteEntry:
    prefix: str
    dest_type: Literal["agent", "controlplane", "authz"]
    target: str  # socket path, http addr, controlplane url, or authz url
    transport: Literal["uds", "http"] = "uds"
    agent: str | None = None
    cp_name: str | None = (
        None  # controlplane name (only set when dest_type == "controlplane")
    )
    whitelist: list[WhitelistEntry] = field(default_factory=list)

    def is_whitelisted(self, path: str) -> bool:
        return any(w.matches(path) for w in self.whitelist)


class RouteRegistry:
    def __init__(self, config_manager: ConfigManager) -> None:
        """Initialize routes from ConfigManager and subscribe to future reloads."""
        cfg = config_manager.current
        self._lock = asyncio.Lock()
        self._cp_prefixes: list[str] = [
            p for cp in cfg.controlplanes for p in cp.prefixes
        ]
        self._authz_prefixes = list(cfg.authz.prefixes) if cfg.authz.url else []
        self._routes: list[RouteEntry] = [
            RouteEntry(
                prefix=p,
                dest_type="controlplane",
                target=cp.url,
                cp_name=cp.name,
            )
            for cp in cfg.controlplanes
            for p in cp.prefixes
        ] + [
            RouteEntry(prefix=p, dest_type="authz", target=cfg.authz.url)
            for p in cfg.authz.prefixes
            if cfg.authz.url
        ]
        self._sort()
        config_manager.subscribe(self._on_config_change)

    async def _on_config_change(self, old: Config, new: Config) -> None:
        """Atomically rebuild routes on config reload, preserving agent routes."""
        new_routes = [
            r
            for r in self.snapshot()
            if r.dest_type not in ("controlplane", "authz")
            and (new.allow_http_agents or r.transport != "http")
        ]
        for cp in new.controlplanes:
            for p in cp.prefixes:
                new_routes.append(
                    RouteEntry(
                        prefix=p,
                        dest_type="controlplane",
                        target=cp.url,
                        cp_name=cp.name,
                    )
                )
        if new.authz.url:
            for p in new.authz.prefixes:
                new_routes.append(
                    RouteEntry(prefix=p, dest_type="authz", target=new.authz.url)
                )
        async with self._lock:
            self._cp_prefixes = [p for cp in new.controlplanes for p in cp.prefixes]
            self._authz_prefixes = list(new.authz.prefixes) if new.authz.url else []
            self._routes = new_routes
            self._sort()

    async def set_controlplanes(
        self, controlplanes: list[tuple[str, str, list[str]]]
    ) -> None:
        """Set or replace all controlplane routing entries.

        Each tuple is (name, url, prefixes).
        """
        async with self._lock:
            self._routes = [r for r in self._routes if r.dest_type != "controlplane"]
            self._cp_prefixes = [
                p for _, _, prefixes in controlplanes for p in prefixes
            ]
            for name, url, prefixes in controlplanes:
                for p in prefixes:
                    self._routes.append(
                        RouteEntry(
                            prefix=p,
                            dest_type="controlplane",
                            target=url,
                            cp_name=name,
                        )
                    )
            self._sort()

    async def set_authz(self, url: str, prefixes: list[str]) -> None:
        """Set or replace the authz routing entries."""
        async with self._lock:
            self._routes = [r for r in self._routes if r.dest_type != "authz"]
            self._authz_prefixes = list(prefixes)
            for p in prefixes:
                self._routes.append(RouteEntry(prefix=p, dest_type="authz", target=url))
            self._sort()

    def controlplane_prefixes(self) -> list[str]:
        """Return a copy of the registered controlplane prefixes."""
        return list(self._cp_prefixes)

    def authz_prefixes(self) -> list[str]:
        """Return a copy of the registered authz prefixes."""
        return list(self._authz_prefixes)

    async def replace_agent_routes(
        self, agent: str, transport: str, target: str, entries: list[dict]
    ) -> tuple[list[str], list[str]]:
        """Atomically replace all routes for an agent. Removes old routes and registers new ones under a single lock."""
        registered: list[str] = []
        rejected: list[str] = []
        async with self._lock:
            self._routes = [r for r in self._routes if r.agent != agent]
            existing_prefixes = {r.prefix for r in self._routes}
            for entry in entries:
                prefix = entry.get("prefix", "")
                if not prefix or prefix in ("/", "/*"):
                    log.error("rejecting invalid prefix %r from %s", prefix, agent)
                    rejected.append(prefix)
                    continue
                if any(self._overlaps(prefix, cp) for cp in self._cp_prefixes):
                    log.error(
                        "rejecting prefix %r from %s: overlaps controlplane",
                        prefix,
                        agent,
                    )
                    rejected.append(prefix)
                    continue
                if any(self._overlaps(prefix, az) for az in self._authz_prefixes):
                    log.error(
                        "rejecting prefix %r from %s: overlaps authz",
                        prefix,
                        agent,
                    )
                    rejected.append(prefix)
                    continue
                if prefix in existing_prefixes:
                    owner = next(
                        (r.agent for r in self._routes if r.prefix == prefix), None
                    )
                    log.error(
                        "rejecting prefix %r from %s: already owned by %s",
                        prefix,
                        agent,
                        owner,
                    )
                    rejected.append(prefix)
                    continue
                wl: list[WhitelistEntry] = []
                for w in entry.get("whitelist", []):
                    match_mode = w.get("match", "exact")
                    if match_mode not in {"exact", "exact_or_prefix", "prefix"}:
                        log.warning(
                            "unknown whitelist match mode %r from %s, defaulting to 'exact'",
                            match_mode,
                            agent,
                        )
                        match_mode = "exact"
                    wl.append(
                        WhitelistEntry(
                            path=w["path"],
                            match=match_mode,
                        )
                    )
                self._routes.append(
                    RouteEntry(
                        prefix=prefix,
                        dest_type="agent",
                        target=target,
                        transport=transport,  # type: ignore[arg-type]
                        agent=agent,
                        whitelist=wl,
                    )
                )
                existing_prefixes.add(prefix)
                registered.append(prefix)
            self._sort()
        return registered, rejected

    async def register_agent_routes(
        self, agent: str, transport: str, target: str, entries: list[dict]
    ) -> tuple[list[str], list[str]]:
        """Register routes for an agent and return (registered, rejected) prefixes."""
        registered: list[str] = []
        rejected: list[str] = []
        async with self._lock:
            existing_prefixes = {r.prefix for r in self._routes}
            for entry in entries:
                prefix = entry.get("prefix", "")
                if not prefix or prefix in ("/", "/*"):
                    log.error("rejecting invalid prefix %r from %s", prefix, agent)
                    rejected.append(prefix)
                    continue
                if any(self._overlaps(prefix, cp) for cp in self._cp_prefixes):
                    log.error(
                        "rejecting prefix %r from %s: overlaps controlplane",
                        prefix,
                        agent,
                    )
                    rejected.append(prefix)
                    continue
                if any(self._overlaps(prefix, az) for az in self._authz_prefixes):
                    log.error(
                        "rejecting prefix %r from %s: overlaps authz",
                        prefix,
                        agent,
                    )
                    rejected.append(prefix)
                    continue
                if prefix in existing_prefixes:
                    owner = next(
                        (r.agent for r in self._routes if r.prefix == prefix), None
                    )
                    log.error(
                        "rejecting prefix %r from %s: already owned by %s",
                        prefix,
                        agent,
                        owner,
                    )
                    rejected.append(prefix)
                    continue
                wl: list[WhitelistEntry] = []
                for w in entry.get("whitelist", []):
                    match_mode = w.get("match", "exact")
                    if match_mode not in {"exact", "exact_or_prefix", "prefix"}:
                        log.warning(
                            "unknown whitelist match mode %r from %s, defaulting to 'exact'",
                            match_mode,
                            agent,
                        )
                        match_mode = "exact"
                    wl.append(
                        WhitelistEntry(
                            path=w["path"],
                            match=match_mode,
                        )
                    )
                self._routes.append(
                    RouteEntry(
                        prefix=prefix,
                        dest_type="agent",
                        target=target,
                        transport=transport,  # type: ignore[arg-type]
                        agent=agent,
                        whitelist=wl,
                    )
                )
                existing_prefixes.add(prefix)
                registered.append(prefix)
            self._sort()
        return registered, rejected

    async def deregister_agent(self, agent: str) -> None:
        """Remove all routes owned by the given agent."""
        async with self._lock:
            self._routes = [r for r in self._routes if r.agent != agent]

    def lookup(self, path: str) -> RouteEntry | None:
        """Find the best matching route entry for the given path."""
        for r in self._routes:
            if path == r.prefix or path.startswith(r.prefix.rstrip("/") + "/"):
                return r
        return None

    def snapshot(self) -> list[RouteEntry]:
        """Return a copy of all current route entries."""
        return list(self._routes)

    async def replace_atomic(self, routes: list[RouteEntry]) -> None:
        """Replace all routes atomically under the lock."""
        async with self._lock:
            self._routes = routes
            self._sort()

    def _sort(self) -> None:
        """Sort routes by prefix length descending for longest-prefix matching."""
        self._routes.sort(key=lambda r: len(r.prefix), reverse=True)

    @staticmethod
    def _overlaps(a: str, b: str) -> bool:
        """Check if two prefixes overlap with each other."""
        a2 = a.rstrip("/") + "/"
        b2 = b.rstrip("/") + "/"
        return a == b or a2.startswith(b2) or b2.startswith(a2)
