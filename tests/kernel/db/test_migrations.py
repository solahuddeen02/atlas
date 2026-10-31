"""ADR-005 §2 and the PLAN-v1 Phase 3 acceptance tests for the schema."""

from pathlib import Path

import psycopg
import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import text

from atlas.kernel.db.engine import make_engine
from atlas.kernel.db.metadata import SCHEMA, metadata
from atlas.kernel.db.migrate import head_revisions, run_migrate
from tests.conftest import PgTestDb

KERNEL_TABLES = {
    "users",
    "account_links",
    "permissions",
    "role_permissions",
    "user_permission_overrides",
    "sessions",
    "audit_log",
    "modules",
    "connectors",
    "outbox",
    "rate_limits",
    "settings",
}
# `*_id` columns that deliberately have no FK (documented in metadata.py).
NO_FK_ALLOWLIST = {("audit_log", "target_id"), ("audit_log", "request_id")}


def test_migrations_upgrade_empty(empty_db: PgTestDb) -> None:
    result = run_migrate(empty_db.settings)
    assert result.before == ()
    assert result.after == head_revisions()
    assert result.dump is None  # nothing to back up on a first install

    with empty_db.superuser_connect() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
                (SCHEMA,),
            )
        }
        assert tables == KERNEL_TABLES | {"alembic_version"}
        kernel_row = conn.execute("SELECT enabled, state FROM kernel.modules WHERE id = 'kernel'")
        assert kernel_row.fetchone() == (True, "ok")


def test_migrate_is_idempotent(migrated_db: PgTestDb) -> None:
    result = run_migrate(migrated_db.settings)
    assert not result.changed
    assert result.dump is None


def test_alembic_check(migrated_db: PgTestDb) -> None:
    engine = make_engine(migrated_db.settings.migrator_database_url)
    try:
        with engine.connect() as conn:
            context = MigrationContext.configure(
                conn,
                opts={
                    "include_schemas": True,
                    "include_name": lambda name, type_, _: type_ != "schema" or name == SCHEMA,
                    "version_table_schema": SCHEMA,
                    "compare_type": True,
                    "compare_server_default": True,
                },
            )
            diff = compare_metadata(context, metadata)
    finally:
        engine.dispose()
    assert diff == []


def test_schema_lint(migrated_db: PgTestDb) -> None:
    with migrated_db.superuser_connect() as conn:
        bad_types = conn.execute(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = %s AND data_type IN "
            "('timestamp without time zone', 'date', 'time without time zone', 'text') "
            "AND (column_name LIKE '%%_at' OR column_name LIKE '%%time%%')",
            (SCHEMA,),
        ).fetchall()
        assert bad_types == [], "timestamps must be timestamptz"

        id_columns = conn.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = %s AND column_name LIKE '%%\\_id' ESCAPE '\\' "
            "AND table_name <> 'alembic_version'",
            (SCHEMA,),
        ).fetchall()
        assert id_columns, "expected *_id columns"
        for table, column in id_columns:
            if (table, column) in NO_FK_ALLOWLIST:
                continue
            fk = conn.execute(
                "SELECT 1 FROM pg_constraint c "
                "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1] "
                "WHERE c.contype = 'f' AND c.conrelid = %s::regclass AND a.attname = %s",
                (f"{SCHEMA}.{table}", column),
            ).fetchone()
            assert fk is not None, f"{table}.{column} has no foreign key"
            index = conn.execute(
                "SELECT 1 FROM pg_index i "
                "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = i.indkey[0] "
                "WHERE i.indrelid = %s::regclass AND a.attname = %s",
                (f"{SCHEMA}.{table}", column),
            ).fetchone()
            assert index is not None, f"{table}.{column} has no index with it as leading column"


def _app_conn(db: PgTestDb) -> psycopg.Connection[object]:
    url = db.settings.database_url.get_secret_value().replace("postgresql+psycopg", "postgresql")
    return psycopg.connect(url, autocommit=True)


@pytest.mark.parametrize(
    "statement",
    [
        "CREATE TABLE kernel.evil (id int)",
        "CREATE TABLE public.evil (id int)",
        "CREATE SCHEMA evil",
        "DROP TABLE kernel.users",
        "ALTER TABLE kernel.users ADD COLUMN evil int",
        "TRUNCATE kernel.users",
        "CREATE INDEX evil ON kernel.users (username)",
    ],
)
def test_app_role_no_ddl(migrated_db: PgTestDb, statement: str) -> None:
    with _app_conn(migrated_db) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute(statement)


def test_app_role_has_dml(migrated_db: PgTestDb) -> None:
    from atlas.kernel.db.ids import uuid7

    with _app_conn(migrated_db) as conn:
        uid = uuid7()
        conn.execute(
            "INSERT INTO kernel.users (id, username, role) VALUES (%s, 'alice', 'adult')", (uid,)
        )
        conn.execute("UPDATE kernel.users SET display_name = 'Alice' WHERE id = %s", (uid,))
        conn.execute("INSERT INTO kernel.outbox (event_type, payload) VALUES ('x', '{}')")
        conn.execute("DELETE FROM kernel.users WHERE id = %s", (uid,))


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE kernel.audit_log SET action = 'tampered'",
        "DELETE FROM kernel.audit_log",
        "TRUNCATE kernel.audit_log",
        "ALTER TABLE kernel.audit_log DISABLE TRIGGER ALL",
    ],
)
def test_audit_log_append_only(migrated_db: PgTestDb, statement: str) -> None:
    with _app_conn(migrated_db) as conn:
        conn.execute("INSERT INTO kernel.audit_log (action, result) VALUES ('t.x', 'allowed')")
        assert conn.execute("SELECT count(*) FROM kernel.audit_log").fetchone() == (1,)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(statement)


def test_constraints_reject_bad_rows(migrated_db: PgTestDb) -> None:
    from atlas.kernel.db.ids import uuid7

    with _app_conn(migrated_db) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO kernel.users (id, username, role) VALUES (%s, 'x', 'root')",
                (uuid7(),),
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO kernel.users (id, username, role, idp_issuer) "
                "VALUES (%s, 'y', 'adult', 'https://id.test')",
                (uuid7(),),
            )
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(
                "INSERT INTO kernel.sessions (id, token_hash, user_id, auth_time, "
                "idle_expires_at, absolute_expires_at) "
                "VALUES (%s, '\\x00', %s, now(), now(), now())",
                (uuid7(), uuid7()),
            )


def test_session_timezone_utc(migrated_db: PgTestDb) -> None:
    for url in (migrated_db.settings.database_url, migrated_db.settings.migrator_database_url):
        engine = make_engine(url)
        try:
            with engine.connect() as conn:
                assert conn.execute(text("SHOW timezone")).scalar() == "UTC"
        finally:
            engine.dispose()


def test_migrations_directory_has_only_kernel_branch() -> None:
    versions = Path(__file__).resolve().parents[3] / "atlas/kernel/migrations/versions"
    assert sorted(p.name for p in versions.glob("*.py")) == ["0001_kernel_baseline.py"]
    assert head_revisions() == ("0001",)
