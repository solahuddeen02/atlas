# core/db/init.py
from __future__ import annotations

from sqlalchemy import text

from core.db.engine import engine
from core.db.models import Base


def init_db() -> None:
    """
    Create tables (dev-friendly). Also performs a lightweight schema upgrade
    for SQLite (until you add Alembic migrations).
    """
    Base.metadata.create_all(bind=engine)

    # Lightweight schema upgrade: add 'status' column if missing.
    # SQLite doesn't support ALTER TABLE ... ADD COLUMN IF NOT EXISTS, so we check.
    with engine.begin() as conn:
        cols = conn.execute(text("PRAGMA table_info(objects);")).fetchall()
        col_names = {c[1] for c in cols}  # (cid, name, type, notnull, dflt_value, pk)

        if "status" not in col_names:
            conn.execute(
                text("ALTER TABLE objects ADD COLUMN status TEXT NOT NULL DEFAULT 'ready';")
            )
