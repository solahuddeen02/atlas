from __future__ import annotations
from core.db.models import Base
from core.db.engine import engine

def init_db() -> None:
    """
    Dev/test only — Alembic handles migrations in production.
    """
    Base.metadata.create_all(bind=engine)