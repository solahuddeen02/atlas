from __future__ import annotations

from abc import ABC, abstractmethod
from types import ModuleType

from sqlalchemy.orm import Session


class PluginRuntime(ABC):
    """Defines how a plugin is executed.

    Implementations:
      - InProcessRuntime  — same process, direct function calls (default)
      - SubprocessRuntime — separate Python process, HTTP proxy
      - ContainerRuntime  — Docker container, HTTP proxy
    """

    @property
    @abstractmethod
    def mode(self) -> str:
        """Runtime identifier: 'inprocess' | 'subprocess' | 'container'."""
        ...

    @abstractmethod
    def start(self, name: str, mod: ModuleType, db: Session) -> None:
        """Called at startup or after install. Subscribe events, recover state, etc."""
        ...

    @abstractmethod
    def get_routers(self, name: str, mod: ModuleType) -> list:
        """Return list of APIRouter (in-process) or proxy routers (out-of-process)."""
        ...

    @abstractmethod
    def stop(self, name: str) -> None:
        """Called on uninstall or shutdown. Release resources."""
        ...
