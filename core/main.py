from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core.auth.router import router as auth_router
from core.db.init import init_db
from core.db.session import SessionLocal
from core.db.engine import engine
from core.runtime import get_runtime
import core.plugin_loader as plugin_loader

_enabled = os.getenv("ATLAS_ENABLED_PLUGINS", "objects,drive,photos,admin,tenant").split(",")
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)


@app.get("/health")
def health():
    return {"status": "ok"}
