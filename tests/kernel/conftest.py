"""Fixtures shared by the session, rate-limit and web-auth tests (Phase 5c-1)."""

import secrets
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Engine, text

from atlas.kernel.config import Settings
from atlas.kernel.db.engine import make_engine
from atlas.kernel.db.ids import uuid7
from atlas.kernel.identity.sessions import SessionStore
from atlas.kernel.identity.tokenbox import TokenBox
from tests.conftest import PgTestDb


class FrozenClock:
    """A clock the test moves by hand. Calling it gives the current frozen time (UTC)."""

    def __init__(self, now: datetime | None = None) -> None:
        self.now = now or datetime(2026, 11, 19, 12, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock()


@pytest.fixture
def app_engine(migrated_db: PgTestDb) -> Iterator[Engine]:
    """An engine as the app role (DML only), on a freshly migrated database."""
    engine = make_engine(migrated_db.settings.database_url, pool_size=30)
    yield engine
    engine.dispose()


MakeUser = Callable[..., UUID]


@pytest.fixture
def make_user(app_engine: Engine) -> MakeUser:
    def make(
        *,
        role: str = "adult",
        status: str = "active",
        expires_at: datetime | None = None,
        username: str | None = None,
    ) -> UUID:
        user_id = uuid7()
        with app_engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO kernel.users "
                    "(id, username, display_name, role, status, expires_at) "
                    "VALUES (:id, :username, :display, :role, :status, :expires_at)"
                ),
                {
                    "id": user_id,
                    "username": username or f"user-{secrets.token_hex(4)}",
                    "display": "Test Person",
                    "role": role,
                    "status": status,
                    "expires_at": expires_at,
                },
            )
        return user_id

    return make


@pytest.fixture
def settings(make_settings: Callable[..., Settings]) -> Settings:
    return make_settings()


@pytest.fixture
def box(settings: Settings) -> TokenBox:
    return TokenBox(settings.secret_key, settings.secret_key_previous)


@pytest.fixture
def store(
    app_engine: Engine, clock: FrozenClock, settings: Settings, box: TokenBox
) -> SessionStore:
    return SessionStore(app_engine, settings.session_lifetimes, box, clock=clock)


def all_text_of_table(engine: Engine, table: str) -> str:
    """Every value of every row of a kernel table, as text, to search for what must not be there."""
    with engine.connect() as connection:
        rows: list[Any] = connection.execute(text(f"SELECT * FROM kernel.{table}")).all()
    return "\n".join(repr(tuple(row)) for row in rows)
