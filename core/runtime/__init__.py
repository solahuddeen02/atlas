from __future__ import annotations

from core.runtime.base import PluginRuntime

SUPPORTED_RUNTIMES = ("inprocess", "subprocess", "container")


def get_runtime(mode: str) -> PluginRuntime:
    """Return a PluginRuntime instance for the given mode."""
    if mode == "inprocess":
        from core.runtime.inprocess import InProcessRuntime
        return InProcessRuntime()
    if mode == "subprocess":
        from core.runtime.subprocess_rt import SubprocessRuntime
        return SubprocessRuntime()
    if mode == "container":
        from core.runtime.container import ContainerRuntime
        return ContainerRuntime()
    raise ValueError(
        f"Unknown runtime mode: {mode!r}. Supported: {SUPPORTED_RUNTIMES}"
    )
