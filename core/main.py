# core/main.py
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from core.api.objects import router as object_router
from core.db.init import init_db
from core.db.session import SessionLocal
from core.domain.objects import recover_incomplete_uploads


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
    
    # Shutdown (ถ้ามี cleanup อื่น ๆ ใส่ตรงนี้)


app = FastAPI(title="Atlas Core", lifespan=lifespan)

app.include_router(object_router)


@app.get("/health")
def health():
    return {"status": "ok"}
