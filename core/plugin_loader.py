from __future__ import annotations

import importlib
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


def install(name: str, db: Session, engine: Any = None) -> dict:
    """Migrate + register in DB. Used at startup for pre-loaded plugins."""
    from core.db.models import InstalledPlugin
    from sqlalchemy import select

    mod = load_module(name)
    manifest: dict = getattr(mod, "manifest", {"name": name, "version": "0.0"})

    existing = db.execute(
        select(InstalledPlugin).where(InstalledPlugin.name == name)
    ).scalar_one_or_none()

    if engine and hasattr(mod, "migrate"):
        mod.migrate(engine)

    if existing:
        return {"name": name, "version": existing.version, "status": "already_installed"}

    db.add(InstalledPlugin(
        name=name,
        version=manifest.get("version", "0.0"),
        package=manifest.get("package"),
        installed_at=_utc_iso(),
    ))
    db.commit()
    return {"name": name, "version": manifest.get("version", "0.0"), "status": "installed"}


def runtime_install(name: str, db: Session, engine: Any = None, app: Any = None) -> dict:
    """Full runtime install: pip install if needed → migrate → startup → mount routes."""
    from core.db.models import InstalledPlugin
    from sqlalchemy import select

    existing = db.execute(
        select(InstalledPlugin).where(InstalledPlugin.name == name)
    ).scalar_one_or_none()
    if existing:
        return {"name": name, "version": existing.version, "status": "already_installed"}

    try:
        mod = load_module(name)
    except ImportError:
        _pip_install(f"{EXTERNAL_PREFIX}{name}")
        mod = importlib.import_module(f"{EXTERNAL_PREFIX}{name}")

    manifest: dict = getattr(mod, "manifest", {"name": name, "version": "0.0"})

    if engine and hasattr(mod, "migrate"):
        mod.migrate(engine)

    if hasattr(mod, "startup"):
        mod.startup(db)

    if app is not None:
        for r in get_routers(mod):
            app.include_router(r)
        app.openapi_schema = None

    db.add(InstalledPlugin(
        name=name,
        version=manifest.get("version", "0.0"),
        package=manifest.get("package"),
        installed_at=_utc_iso(),
    ))
    db.commit()
    return {"name": name, "version": manifest.get("version", "0.0"), "status": "installed"}


def uninstall(name: str, db: Session) -> dict:
    """Remove from DB. Routes stay active until restart."""
    from core.db.models import InstalledPlugin
    from sqlalchemy import delete, select

    existing = db.execute(
        select(InstalledPlugin).where(InstalledPlugin.name == name)
    ).scalar_one_or_none()
    if not existing:
        return {"name": name, "status": "not_found"}

    db.execute(delete(InstalledPlugin).where(InstalledPlugin.name == name))
    db.commit()
    return {"name": name, "status": "uninstalled", "note": "routes remain active until restart"}


def list_installed(db: Session) -> list[dict]:
    from core.db.models import InstalledPlugin
    from sqlalchemy import select
    rows = db.execute(select(InstalledPlugin)).scalars().all()
    return [{"name": r.name, "version": r.version, "package": r.package, "installed_at": r.installed_at} for r in rows]


def get_routers(mod: ModuleType) -> list:
    return getattr(mod, "routers", [])
