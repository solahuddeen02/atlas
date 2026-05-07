from __future__ import annotations

import os
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core.config import ATLAS_DOMAIN
from core.auth.router import router as auth_router
from core.admin.router import router as admin_router
from core.system.router import router as system_router
from core.tenant.router import router as tenant_router
from core.db.init import init_db
from core.db.session import SessionLocal
from core.db.engine import engine
from core.runtime import get_runtime
import core.plugin_loader as plugin_loader

_enabled = os.getenv("ATLAS_ENABLED_PLUGINS", "objects,drive,photos").split(",")
_plugin_names = [n.strip() for n in _enabled if n.strip()]
_plugin_mods: list[tuple] = []

for _name in _plugin_names:
    try:
        _plugin_mods.append((plugin_loader.load_module(_name), _name))
    except ImportError as e:
        print(f"[plugin] failed to import '{_name}': {e}")


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    db = SessionLocal()
    try:
        for mod, name in _plugin_mods:
            plugin_loader.install(name, db, engine=engine)
            mode = plugin_loader.get_installed_runtime(name, db)
            runtime = get_runtime(mode)
            for r in runtime.get_routers(name, mod):
                app.include_router(r)
            runtime.start(name, mod, db)
            print(f"[plugin] loaded: {name} [{mode}]")
    finally:
        db.close()
    yield


app = FastAPI(title="Atlas", lifespan=lifespan)

_domain_re = re.escape(ATLAS_DOMAIN)
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=rf"https?://(localhost(:\d+)?|(.+\.)?{_domain_re}(:\d+)?)",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Core routers — always mounted, not plugin-controlled
app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(system_router)
app.include_router(tenant_router)


@app.get("/health")
def health():
    return {"status": "ok"}
