# core/db/init.py
from __future__ import annotations

from core.db.engine import engine
from core.db.models import Base

def init_db() -> None:
    Base.metadata.create_all(bind=engine)
