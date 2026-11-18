"""Migration 0002: sessions for the relying party (Phase 5c-1)."""

import secrets
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from alembic import command

from atlas.kernel.db.engine import make_engine
from atlas.kernel.db.migrate import alembic_config, current_revisions, run_migrate
from tests.conftest import PgTestDb

NEW_SESSION_COLUMNS = {
    "csrf_token": "text",
    "idp_tokens_enc": "bytea",
    "revalidate_after": "timestamp with time zone",
    "revoked_reason": "text",
}


def upgrade_to(db: PgTestDb, revision: str) -> None:
    """Migrate up to one revision only, as atlas_migrator (the way `atlas-admin migrate` does)."""
    engine = make_engine(db.settings.migrator_database_url, pool_size=1)
    try:
        with engine.connect() as connection:
            command.upgrade(alembic_config(connection), revision)
            connection.commit()
    finally:
        engine.dispose()


def revisions(db: PgTestDb) -> tuple[str, ...]:
    engine = make_engine(db.settings.migrator_database_url, pool_size=1)
    try:
        with engine.connect() as connection:
            return current_revisions(connection)
    finally:
        engine.dispose()


def columns(db: PgTestDb, table: str) -> dict[str, str]:
    with db.superuser_connect() as conn:
        return dict(
            conn.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = 'kernel' AND table_name = %s",
                (table,),
            ).fetchall()
        )


def app_conn(db: PgTestDb) -> psycopg.Connection[Any]:
    url = db.settings.database_url.get_secret_value().replace("postgresql+psycopg", "postgresql")
    return psycopg.connect(url, autocommit=True)


def new_user(conn: psycopg.Connection[Any]) -> str:
    user_id = str(uuid4())
    conn.execute(
        "INSERT INTO kernel.users (id, username, role) VALUES (%s, %s, 'adult')",
        (user_id, f"u-{secrets.token_hex(4)}"),
    )
    return user_id


def new_session(
    conn: psycopg.Connection[Any], user_id: str, *, csrf: bool = True, **extra: str
) -> str:
    """A session row. `csrf=False` is for the baseline schema, which has no such column."""
    session_id = str(uuid4())
    names = ["id", "token_hash", "user_id", "auth_time", "idle_expires_at", "absolute_expires_at"]
    marks = ["%s", "%s", "%s", "now()", "now()", "now()"]
    values: list[Any] = [session_id, secrets.token_bytes(32), user_id]
    if csrf:
        names.append("csrf_token")
        marks.append("%s")
        values.append(secrets.token_urlsafe(32))
    for name, value in extra.items():
        names.append(name)
        marks.append("now()" if value == "now()" else "%s")
        if value != "now()":
            values.append(value)
    conn.execute(
        f"INSERT INTO kernel.sessions ({', '.join(names)}) VALUES ({', '.join(marks)})", values
    )
    return session_id


def new_attempt(conn: psycopg.Connection[Any], **overrides: Any) -> str:
    row: dict[str, Any] = {
        "id": str(uuid4()),
        "cookie_hash": secrets.token_bytes(32),
        "state_hash": secrets.token_bytes(32),
        "nonce": "n",
        "code_verifier_enc": b"\x01\x02",
        "purpose": "login",
        "session_id": None,
    }
    row.update(overrides)
    names = list(row)
    conn.execute(
        f"INSERT INTO kernel.login_attempts ({', '.join(names)}, expires_at) "
        f"VALUES ({', '.join(['%s'] * len(names))}, now() + interval '10 minutes')",
        list(row.values()),
    )
    return str(row["id"])


# --- the upgrade from the current baseline ------------------------------------------------------


def test_the_baseline_has_none_of_it_and_the_upgrade_adds_it(empty_db: PgTestDb) -> None:
    upgrade_to(empty_db, "0001")
    assert revisions(empty_db) == ("0001",)
    before = columns(empty_db, "sessions")
    assert not set(NEW_SESSION_COLUMNS) & set(before)
    assert columns(empty_db, "login_attempts") == {}

    result = run_migrate(empty_db.settings)

    assert (result.before, result.after) == (("0001",), ("0002",))
    assert result.dump is not None and result.dump.name.endswith("-0001.dump")  # backed up first
    after = columns(empty_db, "sessions")
    for name, data_type in NEW_SESSION_COLUMNS.items():
        assert after[name] == data_type
    assert set(before) <= set(after)  # nothing was taken away
    assert set(columns(empty_db, "login_attempts")) == {
        "id", "cookie_hash", "state_hash", "nonce", "code_verifier_enc", "purpose",
        "session_id", "next_path", "prompt_login", "created_at", "expires_at",
    }  # fmt: skip
    assert revisions(empty_db) == ("0002",)
    assert not run_migrate(empty_db.settings).changed  # and a second run is a no-op


def test_it_runs_on_the_empty_table_and_leaves_the_people_alone(empty_db: PgTestDb) -> None:
    upgrade_to(empty_db, "0001")
    with empty_db.superuser_connect() as conn:
        user = new_user(conn)
        assert conn.execute("SELECT count(*) FROM kernel.sessions").fetchone() == (0,)
    run_migrate(empty_db.settings)
    with empty_db.superuser_connect() as conn:
        assert conn.execute("SELECT count(*) FROM kernel.sessions").fetchone() == (0,)
        assert conn.execute(
            "SELECT count(*) FROM kernel.users WHERE id = %s", (user,)
        ).fetchone() == (1,)


def test_a_session_row_left_by_a_pre_release_build_is_cleared(empty_db: PgTestDb) -> None:
    """Nothing creates sessions before 5c, so there are none; if one is there, it goes."""
    upgrade_to(empty_db, "0001")
    with empty_db.superuser_connect() as conn:
        new_session(conn, new_user(conn), csrf=False)  # the baseline has no csrf_token
        assert conn.execute("SELECT count(*) FROM kernel.sessions").fetchone() == (1,)
    run_migrate(empty_db.settings)
    with empty_db.superuser_connect() as conn:
        assert conn.execute("SELECT count(*) FROM kernel.sessions").fetchone() == (0,)


def test_the_live_sessions_index_is_partial(migrated_db: PgTestDb) -> None:
    with migrated_db.superuser_connect() as conn:
        (definition,) = conn.execute(
            "SELECT indexdef FROM pg_indexes WHERE schemaname = 'kernel' "
            "AND indexname = 'ix_sessions_user_id_active'"
        ).fetchone() or (None,)
    assert definition is not None
    assert "(user_id)" in definition and "WHERE (revoked_at IS NULL)" in definition


# --- what the app role may do ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("table", "allowed", "denied"),
    [
        ("sessions", ["SELECT", "INSERT", "UPDATE", "DELETE"], ["TRUNCATE"]),
        (
            "login_attempts",
            ["SELECT", "INSERT", "DELETE"],
            ["UPDATE", "TRUNCATE", "REFERENCES", "TRIGGER"],
        ),
    ],
)
def test_the_app_role_has_only_the_grants_it_needs(
    migrated_db: PgTestDb, table: str, allowed: list[str], denied: list[str]
) -> None:
    with migrated_db.superuser_connect() as conn:
        for privilege in allowed:
            row = conn.execute(
                "SELECT has_table_privilege('atlas_app', %s, %s)", (f"kernel.{table}", privilege)
            ).fetchone()
            assert row == (True,), (table, privilege)
        for privilege in denied:
            row = conn.execute(
                "SELECT has_table_privilege('atlas_app', %s, %s)", (f"kernel.{table}", privilege)
            ).fetchone()
            assert row == (False,), (table, privilege)
        column = conn.execute(
            "SELECT has_any_column_privilege('atlas_app', 'kernel.login_attempts', 'UPDATE')"
        ).fetchone()
        assert column == (False,)  # not even one column


def test_a_login_attempt_can_be_inserted_read_and_consumed_but_not_changed(
    migrated_db: PgTestDb,
) -> None:
    with app_conn(migrated_db) as conn:
        attempt = new_attempt(conn)
        row = conn.execute(
            "SELECT next_path, prompt_login, purpose FROM kernel.login_attempts WHERE id = %s",
            (attempt,),
        ).fetchone()
        assert row == ("/", True, "login")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "UPDATE kernel.login_attempts SET nonce = 'changed' WHERE id = %s", (attempt,)
            )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("TRUNCATE kernel.login_attempts")
        consumed = conn.execute(
            "DELETE FROM kernel.login_attempts WHERE id = %s RETURNING nonce", (attempt,)
        ).fetchall()
        assert consumed == [("n",)]
        # Consumed once: the same delete finds nothing the second time.
        assert (
            conn.execute(
                "DELETE FROM kernel.login_attempts WHERE id = %s RETURNING nonce", (attempt,)
            ).fetchall()
            == []
        )


def test_the_app_role_updates_sessions(migrated_db: PgTestDb) -> None:
    with app_conn(migrated_db) as conn:
        session = new_session(conn, new_user(conn))
        conn.execute(
            "UPDATE kernel.sessions SET revoked_at = now(), revoked_reason = 'logout' "
            "WHERE id = %s",
            (session,),
        )
        conn.execute("DELETE FROM kernel.sessions WHERE id = %s", (session,))


# --- the new constraints --------------------------------------------------------------------------


def test_revoked_at_and_its_reason_go_together(migrated_db: PgTestDb) -> None:
    with app_conn(migrated_db) as conn:
        user = new_user(conn)
        with pytest.raises(psycopg.errors.CheckViolation):
            new_session(conn, user, revoked_at="now()")  # no reason
        with pytest.raises(psycopg.errors.CheckViolation):
            new_session(conn, user, revoked_reason="logout")  # no time
        with pytest.raises(psycopg.errors.CheckViolation):
            new_session(conn, user, revoked_at="now()", revoked_reason="because")
        with pytest.raises(psycopg.errors.CheckViolation):
            new_session(conn, user, revoked_at="now()", revoked_reason="")
        new_session(conn, user)  # live: neither
        for reason in (
            "logout", "user", "admin", "disabled", "role_changed", "expired", "idp_ended",
            "idp_unreachable", "replaced",
        ):  # fmt: skip
            new_session(conn, user, revoked_at="now()", revoked_reason=reason)


def test_a_session_must_have_a_csrf_token(migrated_db: PgTestDb) -> None:
    with app_conn(migrated_db) as conn:
        user = new_user(conn)
        with pytest.raises(psycopg.errors.NotNullViolation):
            new_session(conn, user, csrf=False)
        session = new_session(conn, user)
        with pytest.raises(psycopg.errors.NotNullViolation):
            conn.execute("UPDATE kernel.sessions SET csrf_token = NULL WHERE id = %s", (session,))
    with migrated_db.superuser_connect() as conn:
        nullable = conn.execute(
            "SELECT is_nullable FROM information_schema.columns WHERE table_schema = 'kernel' "
            "AND table_name = 'sessions' AND column_name = 'csrf_token'"
        ).fetchone()
    assert nullable == ("NO",)


def test_a_new_session_has_no_stored_token_and_is_not_revoked(migrated_db: PgTestDb) -> None:
    with app_conn(migrated_db) as conn:
        session = new_session(conn, new_user(conn))
        row = conn.execute(
            "SELECT idp_tokens_enc, revoked_reason, revalidate_after IS NOT NULL "
            "FROM kernel.sessions WHERE id = %s",
            (session,),
        ).fetchone()
        assert row == (None, None, True)


def test_a_login_is_a_login_or_a_step_up_of_a_session(migrated_db: PgTestDb) -> None:
    with app_conn(migrated_db) as conn:
        session = new_session(conn, new_user(conn))
        new_attempt(conn)  # a login: no session
        new_attempt(conn, purpose="step_up", session_id=session)
        with pytest.raises(psycopg.errors.CheckViolation):
            new_attempt(conn, purpose="step_up", session_id=None)
        with pytest.raises(psycopg.errors.CheckViolation):
            new_attempt(conn, purpose="login", session_id=session)
        with pytest.raises(psycopg.errors.CheckViolation):
            new_attempt(conn, purpose="other")
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            new_attempt(conn, purpose="step_up", session_id=str(uuid4()))


def test_the_hashes_of_a_login_are_unique(migrated_db: PgTestDb) -> None:
    with app_conn(migrated_db) as conn:
        cookie, state = secrets.token_bytes(32), secrets.token_bytes(32)
        new_attempt(conn, cookie_hash=cookie, state_hash=state)
        with pytest.raises(psycopg.errors.UniqueViolation):
            new_attempt(conn, cookie_hash=cookie)
        with pytest.raises(psycopg.errors.UniqueViolation):
            new_attempt(conn, state_hash=state)


def test_ending_a_session_takes_its_step_up_attempts_with_it(migrated_db: PgTestDb) -> None:
    with app_conn(migrated_db) as conn:
        user = new_user(conn)
        session = new_session(conn, user)
        attempt = new_attempt(conn, purpose="step_up", session_id=session)
        conn.execute("DELETE FROM kernel.users WHERE id = %s", (user,))  # cascades to the session
        assert conn.execute(
            "SELECT count(*) FROM kernel.login_attempts WHERE id = %s", (attempt,)
        ).fetchone() == (0,)
