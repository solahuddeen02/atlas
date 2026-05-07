from __future__ import annotations

import importlib
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core.auth.router import router as auth_router
from core.db.init import init_db
from core.db.session import SessionLocal

# Import plugin modules before app creation so routers are ready to mount
_enabled = os.getenv("ATLAS_ENABLED_PLUGINS", "objects,drive,photos,admin,tenant").split(",")
_plugins: list[tuple[object, str]] = []

for _name in [n.strip() for n in _enabled if n.strip()]:
    try:
        mod = importlib.import_module(f"plugins.{_name}")
        _plugins.append((mod, _name))
    except ImportError as e:
        print(f"[plugin] failed to import '{_name}': {e}")


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    db = SessionLocal()
    try:
        for mod, _ in _plugins:
            if hasattr(mod, "startup"):
                mod.startup(db)
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

for mod, name in _plugins:
    for r in getattr(mod, "routers", []):
        app.include_router(r)
    print(f"[plugin] loaded: {name}")


@app.get("/health")
def health():
    return {"status": "ok"}
