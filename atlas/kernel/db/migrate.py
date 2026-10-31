"""`atlas-admin migrate` (ADR-005 §3).

1. Take a Postgres advisory lock, so concurrent runs serialize.
2. If revisions are pending and the database already has a schema, write a
   `pg_dump -Fc` to the backup directory. If that fails, stop: nothing is migrated.
3. `alembic upgrade heads` as atlas_migrator.
4. Idempotent seed steps.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, text

import atlas
from atlas.kernel.config import DatabaseSettings
from atlas.kernel.db.engine import make_engine
from atlas.kernel.db.metadata import SCHEMA
from atlas.kernel.db.postgres import run_pg_tool

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
# Arbitrary constant shared by every atlas-admin migrate process ("ATLASMIG").
MIGRATE_LOCK_KEY = 0x41544C41534D4947


class MigrateError(RuntimeError):
    pass


@dataclass
class MigrateResult:
    before: tuple[str, ...]
    after: tuple[str, ...]
    dump: Path | None

    @property
    def changed(self) -> bool:
        return self.before != self.after


def alembic_config(connection: Connection | None = None) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("version_locations", str(MIGRATIONS_DIR / "versions"))
    # TODO(phase-7): append each installed module's migrations directory.
    if connection is not None:
        config.attributes["connection"] = connection
    return config


def head_revisions() -> tuple[str, ...]:
    return tuple(sorted(ScriptDirectory.from_config(alembic_config()).get_heads()))


def current_revisions(connection: Connection) -> tuple[str, ...]:
    context = MigrationContext.configure(connection, opts={"version_table_schema": SCHEMA})
    return tuple(sorted(context.get_current_heads()))


def _write_dump(settings: DatabaseSettings, revisions: tuple[str, ...], now: datetime) -> Path:
    backup_dir = settings.backup_dir
    backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = backup_dir / f"pre-migrate-{stamp}-{'+'.join(revisions)}.dump"
    partial = target.with_suffix(".dump.partial")
    try:
        run_pg_tool(
            "pg_dump",
            settings.migrator_database_url,
            ["--format=custom", f"--schema={SCHEMA}", f"--file={partial}"],
        )
        os.chmod(partial, 0o600)
        partial.rename(target)
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    return target


def seed(connection: Connection) -> None:
    """Idempotent seed steps, run after every successful upgrade."""
    connection.execute(
        text(
            "INSERT INTO kernel.modules (id, enabled, state, installed_version) "
            "VALUES ('kernel', true, 'ok', :version) "
            "ON CONFLICT (id) DO UPDATE SET installed_version = EXCLUDED.installed_version, "
            "updated_at = now()"
        ),
        {"version": atlas.__version__},
    )
    # TODO(phase-6): seed built-in role grants and manifest permissions.
    # TODO(phase-7): registry sync (module rows from installed manifests).


def run_migrate(
    settings: DatabaseSettings,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> MigrateResult:
    engine = make_engine(settings.migrator_database_url, pool_size=1)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": MIGRATE_LOCK_KEY})
            connection.commit()
            try:
                before = current_revisions(connection)
                connection.commit()
                pending = before != head_revisions()
                dump = None
                if pending and before:
                    try:
                        dump = _write_dump(settings, before, now())
                    except Exception as exc:
                        raise MigrateError(
                            f"pre-migration backup failed, nothing was migrated: {exc}"
                        ) from None
                if pending:
                    command.upgrade(alembic_config(connection), "heads")
                    connection.commit()
                seed(connection)
                connection.commit()
                after = current_revisions(connection)
                connection.commit()
            finally:
                connection.rollback()
                connection.execute(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": MIGRATE_LOCK_KEY}
                )
                connection.commit()
    finally:
        engine.dispose()
    return MigrateResult(before=before, after=after, dump=dump)
