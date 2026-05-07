from __future__ import annotations

import importlib
import json
import sys
import subprocess
from datetime import datetime, timezone
from types import ModuleType
from typing import Any

from sqlalchemy.orm import Session

EXTERNAL_PREFIX = "atlas_plugin_"


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_module(name: str) -> ModuleType:
    """Load plugin module — local plugins/ first, then pip package."""
    try:
        return importlib.import_module(f"plugins.{name}")
    except ImportError:
        return importlib.import_module(f"{EXTERNAL_PREFIX}{name}")


def _pip_install(package: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "pip", "install", package],
        check=True, capture_output=True, text=True,
    )


def _get_row(name: str, db: Session):
    from core.db.models import InstalledPlugin
    from sqlalchemy import select
    return db.execute(
        select(InstalledPlugin).where(InstalledPlugin.name == name)
    ).scalar_one_or_none()


def get_dependents(name: str, db: Session) -> list[str]:
    """Return names of installed plugins that declare `name` as a dependency."""
    from core.db.models import InstalledPlugin
    from sqlalchemy import select
    rows = db.execute(select(InstalledPlugin)).scalars().all()
    return [
        r.name for r in rows
        if name in json.loads(r.dependencies or "[]")
    ]


def install(
    name: str,
    db: Session,
    engine: Any = None,
    runtime_mode: str = "inprocess",
    _visited: set[str] | None = None,
) -> dict:
    """Migrate + register plugin. Recursively installs declared dependencies first."""
    if _visited is None:
        _visited = set()
    if name in _visited:
        return {"name": name, "status": "skipped_cycle"}
    _visited.add(name)

    mod = load_module(name)
    manifest: dict = getattr(mod, "manifest", {"name": name, "version": "0.0"})
    deps: list[str] = manifest.get("depends_on", [])

    # Install dependencies first
    for dep in deps:
        if not _get_row(dep, db):
            print(f"[plugin] auto-installing dependency: {dep} (required by {name})")
            install(dep, db, engine=engine, runtime_mode=runtime_mode, _visited=_visited)

    if engine and hasattr(mod, "migrate"):
        mod.migrate(engine)

    existing = _get_row(name, db)
    if existing:
        return {"name": name, "version": existing.version, "runtime": existing.runtime, "status": "already_installed"}

    from core.db.models import InstalledPlugin
    db.add(InstalledPlugin(
        name=name,
        version=manifest.get("version", "0.0"),
        package=manifest.get("package"),
        installed_at=_utc_iso(),
        runtime=runtime_mode,
        dependencies=json.dumps(deps),
    ))
    db.commit()
    return {"name": name, "version": manifest.get("version", "0.0"), "runtime": runtime_mode, "status": "installed"}


def get_installed_runtime(name: str, db: Session) -> str:
    """Return the runtime mode stored in DB for an installed plugin."""
    row = _get_row(name, db)
    return row.runtime if row else "inprocess"


def runtime_install(
    name: str,
    db: Session,
    engine: Any = None,
    app: Any = None,
    runtime_mode: str = "inprocess",
    _visited: set[str] | None = None,
) -> dict:
    """Full runtime install: resolve deps → pip if needed → migrate → start → mount."""
    from core.runtime import get_runtime

    if _visited is None:
        _visited = set()
    if name in _visited:
        return {"name": name, "status": "skipped_cycle"}
    _visited.add(name)

    existing = _get_row(name, db)
    if existing:
        return {"name": name, "version": existing.version, "runtime": existing.runtime, "status": "already_installed"}

    try:
        mod = load_module(name)
    except ImportError:
        _pip_install(f"{EXTERNAL_PREFIX}{name}")
        mod = importlib.import_module(f"{EXTERNAL_PREFIX}{name}")

    manifest: dict = getattr(mod, "manifest", {"name": name, "version": "0.0"})
    deps: list[str] = manifest.get("depends_on", [])

    # Install dependencies first
    for dep in deps:
        if not _get_row(dep, db):
            print(f"[plugin] auto-installing dependency: {dep} (required by {name})")
            runtime_install(dep, db, engine=engine, app=app, runtime_mode=runtime_mode, _visited=_visited)

    if engine and hasattr(mod, "migrate"):
        mod.migrate(engine)

    runtime = get_runtime(runtime_mode)
    runtime.start(name, mod, db)

    if app is not None:
        for r in runtime.get_routers(name, mod):
            app.include_router(r)
        app.openapi_schema = None

    from core.db.models import InstalledPlugin
    db.add(InstalledPlugin(
        name=name,
        version=manifest.get("version", "0.0"),
        package=manifest.get("package"),
        installed_at=_utc_iso(),
        runtime=runtime_mode,
        dependencies=json.dumps(deps),
    ))
    db.commit()
    return {"name": name, "version": manifest.get("version", "0.0"), "runtime": runtime_mode, "status": "installed"}


def uninstall(name: str, db: Session) -> dict:
    """Remove plugin from DB. Blocked if other installed plugins depend on it."""
    existing = _get_row(name, db)
    if not existing:
        return {"name": name, "status": "not_found"}

    dependents = get_dependents(name, db)
    if dependents:
        return {
            "name": name,
            "status": "blocked",
            "reason": f"required by: {', '.join(dependents)}",
            "dependents": dependents,
        }

    from sqlalchemy import delete
    from core.db.models import InstalledPlugin
    db.execute(delete(InstalledPlugin).where(InstalledPlugin.name == name))
    db.commit()
    return {"name": name, "status": "uninstalled", "note": "routes remain active until restart"}


def list_installed(db: Session) -> list[dict]:
    from core.db.models import InstalledPlugin
    from sqlalchemy import select
    rows = db.execute(select(InstalledPlugin)).scalars().all()
    all_names = {r.name for r in rows}

    result = []
    for r in rows:
        deps = json.loads(r.dependencies or "[]")
        dependents = [
            other.name for other in rows
            if r.name in json.loads(other.dependencies or "[]")
        ]
        result.append({
            "name": r.name,
            "version": r.version,
            "package": r.package,
            "runtime": r.runtime,
            "port": r.port,
            "installed_at": r.installed_at,
            "depends_on": deps,
            "used_by": dependents,
            # missing_deps: declared deps that are not installed
            "missing_deps": [d for d in deps if d not in all_names],
        })
    return result


def get_routers(mod: ModuleType) -> list:
    return getattr(mod, "routers", [])
