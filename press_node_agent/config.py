from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, Field, model_validator
from watchfiles import awatch

log = logging.getLogger(__name__)


class Config(BaseModel):
    class Listen(BaseModel):
        host: str = "0.0.0.0"
        port: int = 8080

    class JWKS(BaseModel):
        url: str
        cache_ttl_seconds: int = 300

    class JWT(BaseModel):
        issuer: str
        audience: str

    class Controlplane(BaseModel):
        name: str
        url: str
        prefixes: list[str] = Field(default_factory=list)
        jwt_token: str

    class Authz(BaseModel):
        url: str = ""
        prefixes: list[str] = Field(default_factory=list)
        jwt_token: str = ""

    listen: Listen = Field(default_factory=Listen)
    jwks: JWKS
    jwt: JWT
    allow_http_agents: bool = False
    controlplanes: list[Controlplane] = Field(default_factory=list)
    authz: Authz = Field(default_factory=Authz)
    socket_dir: str = "/run/press-node-agent"
    admin_socket: str = "/run/press-node-agent/node.sock"

    @model_validator(mode="after")
    def _validate_controlplanes(self) -> "Config":
        names: set[str] = set()
        for cp in self.controlplanes:
            if cp.name in names:
                raise ValueError(f"duplicate controlplane name: {cp.name}")
            names.add(cp.name)
        return self


class ConfigManager:
    """Holds the active config and notifies subscribers on reload."""

    def __init__(self) -> None:
        with self.config_path.open() as f:
            data: dict[str, Any] = json.load(f)

        self._config: Config = Config.model_validate(data)
        self._subscribers: list[Callable[[Config, Config], Any]] = []

    @property
    def config_path(self) -> Path:
        """Return the path to the config file."""
        return Path(os.environ.get("NODE_AGENT_CONFIG", "./config.json")).resolve()

    @property
    def current(self) -> Config:
        """Return the current config snapshot."""
        return self._config

    def subscribe(self, fn: Callable[[Config, Config], Any]) -> None:
        """Register a callback to be called on config changes."""
        self._subscribers.append(fn)

    async def watch(self) -> None:
        """Watch the config file for changes and reload on modification."""
        while True:
            path = self.config_path
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
                        new_cfg.listen.host != self._config.listen.host
                        or new_cfg.listen.port != self._config.listen.port
                    ):
                        log.warning(
                            "listen host/port change requires restart, ignoring"
                        )
                        new_cfg.listen = self._config.listen
                    log.info("config reloaded")
                    await self._swap(new_cfg)
            except Exception:
                log.exception("config watcher error, restarting in 5s")
                await asyncio.sleep(5)

    async def _swap(self, new_cfg: Config) -> None:
        """Replace the current config and notify all subscribers."""
        old = self._config
        self._config = new_cfg
        for fn in self._subscribers:
            try:
                res = fn(old, new_cfg)
                if asyncio.iscoroutine(res):
                    await res
            except Exception:
                log.exception("config subscriber failed")
