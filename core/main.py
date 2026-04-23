# core/main.py
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core.apps.drive.router import router as drive_router
from core.apps.objects.router import router as objects_router
from core.apps.photos.router import router as photos_router
from core.db.init import init_db
from core.db.session import SessionLocal
from core.domain.objects import recover_incomplete_uploads
from core.apps.auth.router import router as auth_router

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Dev only: in production, run `alembic upgrade head` before starting
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


app = FastAPI(title="Atlas Core", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(objects_router)
app.include_router(drive_router)
app.include_router(photos_router)
app.include_router(auth_router)

@app.get("/health")
def health():
    return {"status": "ok"}