from __future__ import annotations

import importlib
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core.auth.router import router as auth_router
from core.db.init import init_db
from core.db.session import SessionLocal
from apps.objects.domain import recover_incomplete_uploads


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()

    data_dir = os.getenv("ATLAS_DATA_DIR", "data")
    db = SessionLocal()
    try:
        stats = recover_incomplete_uploads(db, data_dir=data_dir)
        if stats["recovered"] or stats["deleted_files"] or stats["deleted_tmp"]:
            print(f"[startup] recovery: {stats}")
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

# Load apps from ATLAS_ENABLED_APPS env — default to all built-in apps
_enabled = os.getenv("ATLAS_ENABLED_APPS", "objects,drive,photos,admin,tenant").split(",")
for _app_name in _enabled:
    _app_name = _app_name.strip()
    if not _app_name:
        continue
    try:
        mod = importlib.import_module(f"apps.{_app_name}.router")
        routers = getattr(mod, "routers", None) or [mod.router]
        for r in routers:
            app.include_router(r)
        print(f"[app] loaded: {_app_name}")
    except ImportError as e:
        print(f"[app] failed to load '{_app_name}': {e}")


@app.get("/health")
def health():
    return {"status": "ok"}
