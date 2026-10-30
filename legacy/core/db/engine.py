# core/db/engine.py
from __future__ import annotations

import os
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine

# Allow override via env for future-proofing
# Example: sqlite:///./atlas.db  (default)
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./atlas.db")

# SQLite tuning:
# - check_same_thread=False allows usage across threads (FastAPI)
# - timeout helps reduce "database is locked" errors during bursts
connect_args = {}
if DATABASE_URL.startswith("sqlite:"):
    connect_args = {"check_same_thread": False, "timeout": 30}

engine = create_engine(
    DATABASE_URL,
    connect_args=connect_args,
    pool_pre_ping=True,
)

# Apply pragmas for SQLite connections (WAL improves concurrency for reads/writes)
@event.listens_for(Engine, "connect")
def set_sqlite_pragma(dbapi_connection, _connection_record):
    if not DATABASE_URL.startswith("sqlite:"):
        return
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("PRAGMA synchronous=NORMAL;")
    cursor.execute("PRAGMA foreign_keys=ON;")
    cursor.close()
