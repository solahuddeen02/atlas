# core/main.py
from __future__ import annotations

import os

from fastapi import FastAPI

from core.api.objects import router as object_router
from core.db.init import init_db
from core.db.session import SessionLocal
from core.domain.objects import recover_incomplete_uploads

app = FastAPI(title="Atlas Core")


@app.on_event("startup")
def startup() -> None:
    # Initialize database schema (SQLAlchemy)
    init_db()

    # Phase 7A.4: recovery/cleanup for incomplete uploads after crashes
    data_dir = os.getenv("ATLAS_DATA_DIR", "data")
    db = SessionLocal()
    try:
        stats = recover_incomplete_uploads(db, data_dir=data_dir)
        # Keep it simple: print for now (later you can use logging)
        if stats["recovered"] or stats["deleted_files"] or stats["deleted_tmp"]:
            print(f"[startup] recovery: {stats}")
    finally:
        db.close()


app.include_router(object_router)


@app.get("/health")
def health():
    return {"status": "ok"}
