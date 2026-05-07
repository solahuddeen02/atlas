# core/db/engine.py
from __future__ import annotations

import os
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./atlas.db")


def _attach_sqlite_pragmas(eng: Engine) -> None:
    @event.listens_for(eng, "connect")
    def _set(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA synchronous=NORMAL;")
        cursor.execute("PRAGMA foreign_keys=ON;")
        cursor.close()


def make_engine(db_url: str) -> Engine:
    """Create a SQLAlchemy engine with appropriate settings.

    Used by the default engine and by TenantRegistry for isolated-mode tenants.
    """
    connect_args = {}
    if db_url.startswith("sqlite:"):
        connect_args = {"check_same_thread": False, "timeout": 30}
    eng = create_engine(db_url, connect_args=connect_args, pool_pre_ping=True)
    if db_url.startswith("sqlite:"):
        _attach_sqlite_pragmas(eng)
    return eng


engine = make_engine(DATABASE_URL)
