"""`atlas-admin backup` and `atlas-admin restore` (ADR-005 §4).

A backup is a directory `atlas-<UTC timestamp>/` in the backup dir holding
`atlas.dump` (`pg_dump -Fc` of the kernel schema) and `manifest.json`
(revision, time, Atlas version; no secrets).

With the bundled IdP (ADR-001) it also holds `idp.json.gz`: a copy (mode 0600) of the
newest Kanidm online backup from `<backup dir>/idp-online/`, and the manifest records
its Kanidm version, domain UUID and age. Kanidm is restored offline, and only into the
same Kanidm version: `atlas-admin restore` checks the version, stages the file for the
idp container and prints the commands (see `idp_restore_steps`).

Retention keeps the newest backup of each of the last N days and of each of the
last M ISO weeks. It only ever deletes directories whose names it created.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
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

# Bundled IdP (see atlas.kernel.cli.idp for the directories and their owners).
IDP_BACKUP_NAME = "idp.json.gz"
IDP_ONLINE_DIR = "idp-online"
IDP_RESTORE_DIR = "idp-restore"
IDP_CONTAINER_RESTORE_DIR = "/restore"
IDP_CONTAINER_CONFIG = "/etc/atlas-idp/server.toml"
# Kanidm's online backup names: backup-<RFC 3339 UTC>.json.gz (kanidm v1.12.0
# server/core/src/actors/v1_read.rs:217). Restore needs the .gz suffix to decompress.
KANIDM_BACKUP_NAME = re.compile(
    r"^backup-(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d{1,9})?Z\.json\.gz$"
)


class BackupError(RuntimeError):
    pass


@dataclass
class BackupResult:
    path: Path
    pruned: list[Path]
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class IdpRestorePlan:
    """What `atlas-admin restore` does about the bundled IdP for one backup."""

    include: bool
    warnings: tuple[str, ...] = ()


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


# --- Bundled IdP -------------------------------------------------------------------


def kanidm_backup_time(path: Path) -> datetime | None:
    match = KANIDM_BACKUP_NAME.match(path.name)
    if not match:
        return None
    fraction = (match.group(2) or ".0")[:7]  # Python parses at most microseconds
    return datetime.fromisoformat(match.group(1) + fraction).replace(tzinfo=UTC)


def newest_kanidm_backup(online_dir: Path) -> Path | None:
    found = [(ts, p) for p in online_dir.iterdir() if (ts := kanidm_backup_time(p)) and p.is_file()]
    return max(found)[1] if found else None


def read_kanidm_backup_header(path: Path) -> tuple[str, str]:
    """The Kanidm release series and domain UUID recorded in a Kanidm backup.

    Kanidm's backup format V5 holds `version` (the series, e.g. "1.12") and `db_d_uuid`
    (kanidm v1.12.0 server/lib/src/be/dbentry.rs:47-55, be/mod.rs:1007-1016).
    """
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, EOFError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise BackupError(
            f"{path.name} is not a readable Kanidm backup ({type(exc).__name__})"
        ) from None
    version, domain = (
        (data.get("version"), data.get("db_d_uuid")) if isinstance(data, dict) else (None, None)
    )
    if not isinstance(version, str) or not isinstance(domain, str):
        raise BackupError(f"{path.name} is not a Kanidm backup in a known format")
    return version, domain


def _copy_new(src: Path, dest: Path, mode: int) -> None:
    """Copy `src` to a new file `dest` with `mode`; never overwrites."""
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with open(src, "rb") as fin, os.fdopen(fd, "wb") as fout:
            shutil.copyfileobj(fin, fout)
            fout.flush()
            os.fsync(fout.fileno())
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    os.chmod(dest, mode)  # os.open's mode is filtered by the umask


def collect_idp_backup(
    settings: DatabaseSettings, target_dir: Path, started: datetime
) -> tuple[dict[str, object] | None, list[str]]:
    """Copy the newest Kanidm online backup into `target_dir`. Never fails the DB backup."""
    online_dir = settings.backup_dir / IDP_ONLINE_DIR
    try:
        newest = newest_kanidm_backup(online_dir)
    except OSError as exc:
        return None, [
            f"the IdP is NOT in this backup: cannot read {online_dir} ({exc.strerror}). "
            "Is the bundled-idp profile on, and did atlas-idp-init run?"
        ]
    if newest is None:
        return None, [
            f"the IdP is NOT in this backup: Kanidm has not written an online backup to "
            f"{online_dir} yet (ATLAS_IDP_BACKUP_SCHEDULE). Run atlas-backup again later."
        ]
    try:
        version, domain_uuid = read_kanidm_backup_header(newest)
    except BackupError as exc:
        return None, [f"the IdP is NOT in this backup: {exc}"]
    _copy_new(newest, target_dir / IDP_BACKUP_NAME, 0o600)
    created = kanidm_backup_time(newest)
    assert created is not None
    age = max(0, int((started - created).total_seconds()))
    entry: dict[str, object] = {
        "file": IDP_BACKUP_NAME,
        "source": newest.name,
        "created_at": created.isoformat(),
        "age_seconds": age,
        "kanidm_version": version,
        "domain_uuid": domain_uuid,
    }
    warnings = []
    if age > 2 * 24 * 3600:
        warnings.append(
            f"the IdP backup is {age // 3600} hours old ({newest.name}); check that idp is "
            "running and ATLAS_IDP_BACKUP_SCHEDULE"
        )
    return entry, warnings


def check_idp_restore(settings: DatabaseSettings, source: Path) -> IdpRestorePlan:
    """Decide what happens to the IdP before anything is restored. Raises on a mismatch."""
    manifest_path = source / MANIFEST_NAME
    if not manifest_path.is_file():
        return IdpRestorePlan(include=False)  # run_restore reports the missing file
    idp = json.loads(manifest_path.read_text()).get("idp")
    if not settings.bundled_idp:
        if idp:
            return IdpRestorePlan(
                include=False,
                warnings=("this backup holds IdP data, but the bundled IdP is off; skipped",),
            )
        return IdpRestorePlan(include=False)
    if not idp:
        return IdpRestorePlan(
            include=False,
            warnings=(
                "this backup has no IdP data (made without the bundled IdP, or before "
                "Kanidm's first online backup): the IdP keeps its current data",
            ),
        )
    if not (source / IDP_BACKUP_NAME).is_file():
        raise BackupError(f"{source} lists IdP data but {IDP_BACKUP_NAME} is missing")
    current = settings.idp_kanidm_series
    if current is None:
        raise BackupError(
            "ATLAS_IDP_KANIDM_SERIES is not set, so the Kanidm version can't be checked "
            "(run restore through docker compose, which sets it)"
        )
    if idp.get("kanidm_version") != current:
        raise BackupError(
            f"the IdP data in this backup is from Kanidm {idp.get('kanidm_version')}, but "
            f"this stack runs Kanidm {current}. Kanidm restores only into the same version: "
            "use the Atlas release (idp image) the backup was made with. Nothing was changed."
        )
    return IdpRestorePlan(include=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stage_idp_restore(settings: DatabaseSettings, source: Path) -> Path:
    """Copy the backup's IdP data where the idp container can read it (0640, idp group).

    The name keeps the `.gz` suffix: Kanidm decompresses only files named `*.gz`.
    """
    staging = settings.backup_dir / IDP_RESTORE_DIR
    if not staging.is_dir():
        raise BackupError(f"{staging} does not exist (atlas-idp-init creates it)")
    src = source / IDP_BACKUP_NAME
    dest = staging / f"{source.name}-{IDP_BACKUP_NAME}"
    if dest.exists():
        if _sha256(dest) != _sha256(src):
            raise BackupError(f"{dest} exists with other content; remove it and re-run")
        return dest
    _copy_new(src, dest, 0o640)
    return dest


def idp_restore_steps(staged: Path) -> list[str]:
    """The offline Kanidm restore, as commands to run in deploy/."""
    inside = f"{IDP_CONTAINER_RESTORE_DIR}/{staged.name}"
    return [
        "docker compose stop idp",
        "docker compose run --rm --no-deps idp /sbin/kanidmd database restore "
        f"-c {IDP_CONTAINER_CONFIG} {inside}",
        "docker compose start idp",
    ]


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
        manifest: dict[str, object] = {
            "format": 1,
            "created_at": started.isoformat(),
            "atlas_version": atlas.__version__,
            "revisions": list(revisions),
            "schemas": [SCHEMA],
        }
        warnings: list[str] = []
        if settings.bundled_idp:
            manifest["idp"], warnings = collect_idp_backup(settings, partial, started)
        (partial / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n")
        partial.rename(target)
    except Exception:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    pruned = prune(backup_dir, started, settings.backup_keep_daily, settings.backup_keep_weekly)
    return BackupResult(path=target, pruned=pruned, warnings=warnings)


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
