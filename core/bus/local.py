from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable


class EventBus:
    def __init__(self):
        self._handlers: dict[str, list[Callable]] = defaultdict(list)

    def subscribe(self, event: str, handler: Callable[[Any], None]) -> None:
        self._handlers[event].append(handler)

    def publish(self, event: str, payload: Any = None) -> None:
        for handler in self._handlers.get(event, []):
            try:
                handler(payload)
            except Exception as e:
                print(f"[bus] handler error on '{event}': {e}")
