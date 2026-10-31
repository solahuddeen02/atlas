"""db-init, migrate (lock, pre-migration dump), backup/restore and the unit of work."""

import json
import stat
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from atlas.kernel.db import UnitOfWork, make_engine
from atlas.kernel.db import migrate as migrate_mod
from atlas.kernel.db.backup import BackupError, run_backup, run_restore, select_retained
from atlas.kernel.db.bootstrap import DbInitError, run_db_init
from atlas.kernel.db.migrate import MigrateError, current_revisions, run_migrate
from tests.conftest import PgTestDb

EXTRA_MIGRATION = """
revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

from alembic import op


def upgrade():
    op.execute("CREATE TABLE kernel.extra_test (id int PRIMARY KEY)")
    op.execute("GRANT SELECT ON kernel.extra_test TO atlas_app")


def downgrade():
    op.execute("DROP TABLE kernel.extra_test")
"""


@pytest.fixture
def pending_revision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Make one extra revision (0002) available, as a future release would."""
    extra = tmp_path / "extra_versions"
    extra.mkdir()
    (extra / "0002_extra.py").write_text(EXTRA_MIGRATION)
    original = migrate_mod.alembic_config

    def with_extra(connection=None):  # type: ignore[no-untyped-def]
        config = original(connection)
        locations = config.get_main_option("version_locations")
        config.set_main_option("version_locations", f"{locations} {extra}")
        return config

    monkeypatch.setattr(migrate_mod, "alembic_config", with_extra)
    return "0002"


def revisions(db: PgTestDb) -> tuple[str, ...]:
    engine = make_engine(db.settings.migrator_database_url)
    try:
        with engine.connect() as conn:
            return current_revisions(conn)
    finally:
        engine.dispose()


# migrate


def test_migrate_dumps_before_pending(migrated_db: PgTestDb, pending_revision: str) -> None:
    result = run_migrate(migrated_db.settings)
    assert result.before == ("0001",)
    assert result.after == (pending_revision,)
    assert result.dump is not None
    assert result.dump.parent == migrated_db.settings.backup_dir
    assert result.dump.name.startswith("pre-migrate-") and result.dump.name.endswith("-0001.dump")
    assert result.dump.stat().st_size > 0
    assert stat.S_IMODE(result.dump.stat().st_mode) == 0o600


def test_migrate_aborts_if_dump_fails(
    migrated_db: PgTestDb,
    pending_revision: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake = fake_bin / "pg_dump"
    fake.write_text("#!/bin/sh\necho 'pg_dump: error: disk full' >&2\nexit 1\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake_bin))

    with pytest.raises(MigrateError, match="pre-migration backup failed, nothing was migrated"):
        run_migrate(migrated_db.settings)
    assert revisions(migrated_db) == ("0001",)
    assert not list(migrated_db.settings.backup_dir.glob("*.partial"))


def test_migrate_lock_serializes(migrated_db: PgTestDb) -> None:
    # Hold the advisory lock from another session; migrate must wait for it.
    engine = make_engine(migrated_db.settings.migrator_database_url)
    finished = threading.Event()
    with engine.connect() as holder:
        holder.execute(text("SELECT pg_advisory_lock(:k)"), {"k": migrate_mod.MIGRATE_LOCK_KEY})
        holder.commit()
        worker = threading.Thread(
            target=lambda: (run_migrate(migrated_db.settings), finished.set())
        )
        worker.start()
        time.sleep(1.0)
        assert not finished.is_set(), "migrate ran while another process held the lock"
        holder.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": migrate_mod.MIGRATE_LOCK_KEY})
        holder.commit()
        worker.join(timeout=30)
    engine.dispose()
    assert finished.is_set()


# db-init


def test_db_init_idempotent(empty_db: PgTestDb) -> None:
    report = run_db_init(empty_db.db_init)
    assert "kept role atlas_migrator" in report.lines
    assert "kept role atlas_app" in report.lines
    assert f"kept database {empty_db.name}" in report.lines


def test_db_init_applies_changed_password(empty_db: PgTestDb) -> None:
    with empty_db.superuser_connect() as conn:
        conn.execute("ALTER ROLE atlas_app PASSWORD 'something-else-entirely'")
    report = run_db_init(empty_db.db_init)
    assert "updated password for role atlas_app" in report.lines
    assert "kept role atlas_migrator" in report.lines
    engine = make_engine(empty_db.settings.database_url)
    try:
        with engine.connect() as conn:
            assert conn.execute(text("SELECT current_user")).scalar() == "atlas_app"
    finally:
        engine.dispose()


def test_db_init_never_prints_passwords(empty_db: PgTestDb) -> None:
    report = "\n".join(run_db_init(empty_db.db_init).lines)
    for secret in (
        empty_db.cluster.app_password,
        empty_db.cluster.migrator_password,
        empty_db.cluster.password_file.read_text().strip(),
    ):
        assert secret not in report


def test_db_init_roles_are_unprivileged(empty_db: PgTestDb) -> None:
    with empty_db.superuser_connect() as conn:
        rows = conn.execute(
            "SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolreplication "
            "FROM pg_roles WHERE rolname IN ('atlas_app', 'atlas_migrator') ORDER BY rolname"
        ).fetchall()
    assert rows == [
        ("atlas_app", False, False, False, False),
        ("atlas_migrator", False, False, False, False),
    ]


def test_db_init_rejects_wrong_role_names(empty_db: PgTestDb) -> None:
    bad = empty_db.db_init.model_copy(
        update={"database_url": empty_db.db_init.migrator_database_url}
    )
    with pytest.raises(DbInitError, match="DATABASE_URL: must connect as atlas_app"):
        run_db_init(bad)


def test_db_init_requires_migrator_url(empty_db: PgTestDb) -> None:
    bad = empty_db.db_init.model_copy(update={"migrator_database_url_override": None})
    with pytest.raises(DbInitError, match="ATLAS_MIGRATOR_DATABASE_URL: is required"):
        run_db_init(bad)


# backup / restore


def test_backup_restore_roundtrip(migrated_db: PgTestDb) -> None:
    engine = make_engine(migrated_db.settings.database_url)
    try:
        with UnitOfWork(engine) as session:
            session.execute(
                text("INSERT INTO kernel.settings (key, value) VALUES ('k', '\"before\"')")
            )
        result = run_backup(migrated_db.settings)
        manifest = json.loads((result.path / "manifest.json").read_text())
        assert manifest["revisions"] == ["0001"]
        assert stat.S_IMODE((result.path / "atlas.dump").stat().st_mode) == 0o600
        assert "password" not in json.dumps(manifest).lower()

        with UnitOfWork(engine) as session:
            session.execute(text("UPDATE kernel.settings SET value = '\"after\"'"))
            session.execute(
                text("INSERT INTO kernel.audit_log (action, result) VALUES ('x', 'allowed')")
            )

        assert run_restore(migrated_db.settings, result.path) == ("0001",)
        with engine.connect() as conn:
            assert conn.execute(text("SELECT value FROM kernel.settings")).scalar() == "before"
            assert conn.execute(text("SELECT count(*) FROM kernel.audit_log")).scalar() == 0
            # Grants survive the restore: the app is still append-only on audit_log.
            conn.execute(
                text("INSERT INTO kernel.audit_log (action, result) VALUES ('y', 'allowed')")
            )
            conn.commit()
    finally:
        engine.dispose()
    with pytest.raises(Exception, match="permission denied"):
        _app_delete_audit(migrated_db)


def _app_delete_audit(db: PgTestDb) -> None:
    engine = make_engine(db.settings.database_url)
    try:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM kernel.audit_log"))
    finally:
        engine.dispose()


def test_restore_rejects_non_backup(migrated_db: PgTestDb, tmp_path: Path) -> None:
    with pytest.raises(BackupError, match="is not an Atlas backup"):
        run_restore(migrated_db.settings, tmp_path)


def test_backup_prunes_by_retention(migrated_db: PgTestDb) -> None:
    settings = migrated_db.settings.model_copy(
        update={"backup_keep_daily": 2, "backup_keep_weekly": 0}
    )
    start = datetime(2026, 10, 1, 3, 0, tzinfo=UTC)
    paths = []
    for days in range(4):
        moment = start + timedelta(days=days)
        paths.append(run_backup(settings, now=lambda moment=moment: moment).path)
    remaining = sorted(p.name for p in settings.backup_dir.iterdir() if p.name.startswith("atlas-"))
    assert remaining == [paths[2].name, paths[3].name]


def test_backup_leaves_unknown_files_alone(migrated_db: PgTestDb) -> None:
    settings = migrated_db.settings.model_copy(
        update={"backup_keep_daily": 1, "backup_keep_weekly": 0}
    )
    settings.backup_dir.mkdir(parents=True, exist_ok=True)
    keep = settings.backup_dir / "my-notes.txt"
    keep.write_text("mine")
    (settings.backup_dir / "atlas-manual").mkdir()
    run_backup(settings)
    assert keep.exists()
    assert (settings.backup_dir / "atlas-manual").exists()


def test_select_retained_daily_and_weekly() -> None:
    now = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)  # a Saturday
    backups = [now - timedelta(days=d, hours=h) for d in range(0, 40) for h in (0, 6)]
    keep = select_retained(backups, now, daily=7, weekly=4)
    days = {ts.date() for ts in keep}
    # Newest of each of the last 7 days...
    for d in range(7):
        assert (now - timedelta(days=d)).date() in days
    # ...plus the newest of each of the 4 most recent ISO weeks; nothing older.
    assert min(keep) >= now - timedelta(days=28)
    assert len(keep) <= 7 + 4
    # One per day at most: the newest of that day.
    for ts in keep:
        same_day = [b for b in backups if b.date() == ts.date()]
        assert ts == max(same_day)


# unit of work


def test_uow_commits_and_rolls_back(migrated_db: PgTestDb) -> None:
    engine = make_engine(migrated_db.settings.database_url)
    try:
        with UnitOfWork(engine) as session:
            session.execute(text("INSERT INTO kernel.settings (key, value) VALUES ('a', '1')"))
        with pytest.raises(RuntimeError), UnitOfWork(engine) as session:
            session.execute(text("INSERT INTO kernel.settings (key, value) VALUES ('b', '2')"))
            raise RuntimeError("boom")
        with engine.connect() as conn:
            keys = conn.execute(text("SELECT key FROM kernel.settings ORDER BY key")).scalars()
            assert list(keys) == ["a"]
    finally:
        engine.dispose()
