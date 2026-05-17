from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, Field
from watchfiles import awatch

log = logging.getLogger(__name__)


class ListenConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8080


class JWKSConfig(BaseModel):
    url: str
    cache_ttl_seconds: int = 300


class JWTConfig(BaseModel):
    issuer: str
    audience: str


class ControlplaneConfig(BaseModel):
    url: str
    prefixes: list[str] = Field(default_factory=list)


class AuthzConfig(BaseModel):
    url: str
    prefixes: list[str] = Field(default_factory=list)


class Config(BaseModel):
    listen: ListenConfig = ListenConfig()
    jwks: JWKSConfig
    jwt: JWTConfig
    node_jwt: str
    allow_http_agents: bool = False
    controlplane: ControlplaneConfig
    authz: AuthzConfig | None = None
    socket_dir: str = "/run/press-node-agent"
    admin_socket: str = "/run/press-node-agent/node.sock"


def _load_path() -> Path:
    return Path(os.environ.get("NODE_AGENT_CONFIG", "./config.json")).resolve()


def load_config() -> Config:
    p = _load_path()
    with p.open() as f:
        data: dict[str, Any] = json.load(f)
    return Config.model_validate(data)


class ConfigHolder:
    """Holds the active config and notifies subscribers on reload."""

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._subs: list[Callable[[Config, Config], Any]] = []

    @property
    def current(self) -> Config:
        return self._cfg

    def subscribe(self, fn: Callable[[Config, Config], Any]) -> None:
        self._subs.append(fn)

    async def _swap(self, new_cfg: Config) -> None:
        old = self._cfg
        self._cfg = new_cfg
        for fn in self._subs:
            try:
                res = fn(old, new_cfg)
                if asyncio.iscoroutine(res):
                    await res
            except Exception:
                log.exception("config subscriber failed")

    async def watch(self) -> None:
        while True:
            path = _load_path()
            try:
                async for _ in awatch(str(path)):
                    try:
                        with path.open() as f:
                            data = json.load(f)
                        new_cfg = Config.model_validate(data)
                    except Exception:
                        log.exception("invalid config on reload, keeping previous")
                        continue
                    # non-reloadable: listen host/port
                    if (
                        new_cfg.listen.host != self._cfg.listen.host
                        or new_cfg.listen.port != self._cfg.listen.port
                    ):
                        log.warning("listen host/port change requires restart, ignoring")
                        new_cfg.listen = self._cfg.listen
                    log.info("config reloaded")
                    await self._swap(new_cfg)
            except Exception:
                log.exception("config watcher error, restarting in 5s")
                await asyncio.sleep(5)
