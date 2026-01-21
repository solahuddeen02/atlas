# core/db/session.py
from __future__ import annotations

from sqlalchemy.orm import sessionmaker
from core.db.engine import engine

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
)

def get_db():
    """
    FastAPI dependency: one DB session per request.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
