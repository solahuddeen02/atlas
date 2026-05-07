from __future__ import annotations

from types import ModuleType

from sqlalchemy.orm import Session

from core.runtime.base import PluginRuntime


class InProcessRuntime(PluginRuntime):
    """Runs the plugin inside the same Python process. Default runtime."""

    @property
    def mode(self) -> str:
        return "inprocess"

    def start(self, name: str, mod: ModuleType, db: Session) -> None:
        if hasattr(mod, "startup"):
            mod.startup(db)

    def get_routers(self, name: str, mod: ModuleType) -> list:
        return getattr(mod, "routers", [])

    def stop(self, name: str) -> None:
        pass
