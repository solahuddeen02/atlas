from __future__ import annotations

from types import ModuleType

from sqlalchemy.orm import Session

from core.runtime.base import PluginRuntime


class ContainerRuntime(PluginRuntime):
    """Plugin runs in a Docker container. Requests are proxied via httpx.

    Planned implementation:
      1. docker run atlas_plugin_{name} --port {port}
      2. Mount a catch-all proxy router: GET /prefix/* → http://container:{port}/*
      3. Health-check before marking ready
    """

    @property
    def mode(self) -> str:
        return "container"

    def start(self, name: str, mod: ModuleType, db: Session) -> None:
        raise NotImplementedError(
            f"ContainerRuntime is not yet implemented. "
            f"Install plugin '{name}' with runtime='inprocess' instead."
        )

    def get_routers(self, name: str, mod: ModuleType) -> list:
        raise NotImplementedError

    def stop(self, name: str) -> None:
        raise NotImplementedError
