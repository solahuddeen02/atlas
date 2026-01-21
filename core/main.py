# core/main.py
from fastapi import FastAPI

from core.db.init import init_db
from core.api.objects import router as object_router

app = FastAPI(title="Atlas Core")

@app.on_event("startup")
def startup() -> None:
    # Initialize database schema (SQLAlchemy)
    init_db()

app.include_router(object_router)

@app.get("/health")
def health():
    return {"status": "ok"}
