"""`atlas-admin backup` and `atlas-admin restore` (ADR-005 §4).

A backup is a directory `atlas-<UTC timestamp>/` in the backup dir holding
`atlas.dump` (`pg_dump -Fc` of the kernel schema) and `manifest.json`
(revision, time, Atlas version; no secrets).

Retention keeps the newest backup of each of the last N days and of each of the
last M ISO weeks. It only ever deletes directories whose names it created.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import text

import atlas
from atlas.kernel.config import DatabaseSettings
from atlas.kernel.db.engine import make_engine
from atlas.kernel.db.metadata import SCHEMA
from atlas.kernel.db.migrate import current_revisions
from atlas.kernel.db.postgres import run_pg_tool

BACKUP_NAME = re.compile(r"^atlas-(\d{8}T\d{6}Z)$")
TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"
DUMP_NAME = "atlas.dump"
MANIFEST_NAME = "manifest.json"
# TODO(phase-7): include each installed module's schema in the dump.
# TODO(spike-s): back up and restore the bundled IdP alongside the database.


class BackupError(RuntimeError):
    pass


@dataclass
class BackupResult:
    path: Path
    pruned: list[Path]


def _revisions(settings: DatabaseSettings) -> tuple[str, ...]:
    engine = make_engine(settings.migrator_database_url, pool_size=1)
    try:
        with engine.connect() as connection:
            return current_revisions(connection)
    finally:
        engine.dispose()


def backup_time(path: Path) -> datetime | None:
    match = BACKUP_NAME.match(path.name)
    if not match or not path.is_dir():
        return None
    return datetime.strptime(match.group(1), TIMESTAMP_FORMAT).replace(tzinfo=UTC)


def select_retained(
    backups: list[datetime], now: datetime, daily: int, weekly: int
) -> set[datetime]:
    """The backups to keep: newest per day for `daily` days, newest per ISO week for `weekly`."""
    keep: set[datetime] = set()
    newest_first = sorted(backups, reverse=True)
    today = now.astimezone(UTC).date()

    days = {today - timedelta(days=i) for i in range(daily)}
    seen_days: set[object] = set()
    for ts in newest_first:
        day = ts.date()
        if day in days and day not in seen_days:
            keep.add(ts)
            seen_days.add(day)

    monday = today - timedelta(days=today.weekday())
    weeks = {(monday - timedelta(weeks=i)).isocalendar()[:2] for i in range(weekly)}
    seen_weeks: set[object] = set()
    for ts in newest_first:
        week = ts.date().isocalendar()[:2]
        if week in weeks and week not in seen_weeks:
            keep.add(ts)
            seen_weeks.add(week)
    return keep


def prune(backup_dir: Path, now: datetime, daily: int, weekly: int) -> list[Path]:
    found = {ts: p for p in backup_dir.iterdir() if (ts := backup_time(p)) is not None}
    keep = select_retained(list(found), now, daily, weekly)
    removed = []
    for ts, path in sorted(found.items()):
        if ts not in keep:
            shutil.rmtree(path)
            removed.append(path)
    return removed


def run_backup(
    settings: DatabaseSettings,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> BackupResult:
    started = now().astimezone(UTC).replace(microsecond=0)
    backup_dir = settings.backup_dir
    backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = backup_dir / f"atlas-{started.strftime(TIMESTAMP_FORMAT)}"
    if target.exists():
        raise BackupError(f"{target} already exists")
    partial = backup_dir / f".{target.name}.partial"
    shutil.rmtree(partial, ignore_errors=True)
    partial.mkdir(mode=0o700)
    try:
        revisions = _revisions(settings)
        run_pg_tool(
            "pg_dump",
            settings.migrator_database_url,
            ["--format=custom", f"--schema={SCHEMA}", f"--file={partial / DUMP_NAME}"],
        )
        os.chmod(partial / DUMP_NAME, 0o600)
        manifest = {
            "format": 1,
            "created_at": started.isoformat(),
            "atlas_version": atlas.__version__,
            "revisions": list(revisions),
            "schemas": [SCHEMA],
        }
        (partial / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n")
        partial.rename(target)
    except Exception:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    pruned = prune(backup_dir, started, settings.backup_keep_daily, settings.backup_keep_weekly)
    return BackupResult(path=target, pruned=pruned)


def run_restore(settings: DatabaseSettings, source: Path) -> tuple[str, ...]:
    """Restore a backup directory into the configured database. Stop atlas-api first."""
    dump = source / DUMP_NAME
    manifest_path = source / MANIFEST_NAME
    if not dump.is_file() or not manifest_path.is_file():
        raise BackupError(
            f"{source} is not an Atlas backup (needs {DUMP_NAME} and {MANIFEST_NAME})"
        )
    manifest = json.loads(manifest_path.read_text())
    expected = tuple(sorted(manifest.get("revisions", [])))

    run_pg_tool(
        "pg_restore",
        settings.migrator_database_url,
        ["--clean", "--if-exists", "--single-transaction", "--exit-on-error", str(dump)],
    )
    restored = _revisions(settings)
    if restored != expected:
        raise BackupError(
            f"restored revision {restored or '(none)'} does not match the backup's {expected}"
        )
    _check_connectable(settings)
    return restored


def _check_connectable(settings: DatabaseSettings) -> None:
    engine = make_engine(settings.database_url, pool_size=1)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT count(*) FROM kernel.modules"))
    finally:
        engine.dispose()
