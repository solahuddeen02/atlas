from __future__ import annotations

from types import ModuleType

from sqlalchemy.orm import Session

from core.runtime.base import PluginRuntime


class SubprocessRuntime(PluginRuntime):
    """Plugin runs as a separate Python process. Requests are proxied via httpx.

    Planned implementation:
      1. Spawn: python -m atlas_plugin_{name} --port {port}
      2. Mount a catch-all proxy router: GET /prefix/* → http://localhost:{port}/*
      3. Health-check before marking ready
    """

    @property
    def mode(self) -> str:
        return "subprocess"

    def start(self, name: str, mod: ModuleType, db: Session) -> None:
        raise NotImplementedError(
            f"SubprocessRuntime is not yet implemented. "
            f"Install plugin '{name}' with runtime='inprocess' instead."
        )

    def get_routers(self, name: str, mod: ModuleType) -> list:
        raise NotImplementedError

    def stop(self, name: str) -> None:
        raise NotImplementedError
