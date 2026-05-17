from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Literal

log = logging.getLogger(__name__)

MatchMode = Literal["exact", "exact_or_prefix", "prefix"]


@dataclass
class WhitelistEntry:
    path: str
    match: MatchMode = "exact"

    def matches(self, path: str) -> bool:
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
    whitelist: list[WhitelistEntry] = field(default_factory=list)

    def is_whitelisted(self, path: str) -> bool:
        return any(w.matches(path) for w in self.whitelist)


class RouteRegistry:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._routes: list[RouteEntry] = []
        self._cp_prefixes: list[str] = []
        self._authz_prefixes: list[str] = []

    async def set_controlplane(self, url: str, prefixes: list[str]) -> None:
        async with self._lock:
            self._routes = [r for r in self._routes if r.dest_type != "controlplane"]
            self._cp_prefixes = list(prefixes)
            for p in prefixes:
                self._routes.append(
                    RouteEntry(prefix=p, dest_type="controlplane", target=url)
                )
            self._sort()

    async def set_authz(self, url: str, prefixes: list[str]) -> None:
        async with self._lock:
            self._routes = [r for r in self._routes if r.dest_type != "authz"]
            self._authz_prefixes = list(prefixes)
            for p in prefixes:
                self._routes.append(RouteEntry(prefix=p, dest_type="authz", target=url))
            self._sort()

    def controlplane_prefixes(self) -> list[str]:
        return list(self._cp_prefixes)

    def authz_prefixes(self) -> list[str]:
        return list(self._authz_prefixes)

    async def register_agent_routes(
        self, agent: str, transport: str, target: str, entries: list[dict]
    ) -> tuple[list[str], list[str]]:
        """Register routes for an agent. Returns (registered, rejected)."""
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
                    wl.append(
                        WhitelistEntry(
                            path=w["path"],
                            match=w.get("match", "exact"),
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
        async with self._lock:
            self._routes = [r for r in self._routes if r.agent != agent]

    def lookup(self, path: str) -> RouteEntry | None:
        for r in self._routes:
            if path == r.prefix or path.startswith(r.prefix.rstrip("/") + "/"):
                return r
        return None

    def snapshot(self) -> list[RouteEntry]:
        return list(self._routes)

    async def replace_atomic(self, routes: list[RouteEntry]) -> None:
        async with self._lock:
            self._routes = routes
            self._sort()

    def _sort(self) -> None:
        self._routes.sort(key=lambda r: len(r.prefix), reverse=True)

    @staticmethod
    def _overlaps(a: str, b: str) -> bool:
        a2 = a.rstrip("/") + "/"
        b2 = b.rstrip("/") + "/"
        return a == b or a2.startswith(b2) or b2.startswith(a2)
