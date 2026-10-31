"""SQLAlchemy engines built from Settings. Nothing here reads the environment."""

from __future__ import annotations

from pydantic import SecretStr
from sqlalchemy import Engine, create_engine


def make_engine(url: SecretStr, *, pool_size: int = 5) -> Engine:
    """An engine whose every connection uses UTC (ADR-005 §1, "Time")."""
    return create_engine(
        url.get_secret_value(),
        pool_pre_ping=True,
        pool_size=pool_size,
        connect_args={"options": "-c timezone=UTC"},
        # Never echo SQL: statements can carry secrets as parameters.
        echo=False,
        hide_parameters=True,
    )
