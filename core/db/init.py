# core/db/init.py
from __future__ import annotations

from sqlalchemy import text
from core.db.engine import engine
from core.db.models import Base

def init_db() -> None:
    Base.metadata.create_all(bind=engine)

    # Lightweight schema upgrade for dev (until Alembic is added)
    with engine.begin() as conn:
        # check if 'status' column exists; if not, add it
        cols = conn.execute(text("PRAGMA table_info(objects);")).fetchall()
        col_names = {c[1] for c in cols}  # (cid, name, type, notnull, dflt_value, pk)

        if "status" not in col_names:
            conn.execute(text("ALTER TABLE objects ADD COLUMN status TEXT NOT NULL DEFAULT 'ready';"))
