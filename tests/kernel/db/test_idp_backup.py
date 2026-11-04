"""Bundled IdP in `atlas-admin backup` / `restore` (PLAN Phase 5a; ADR-001).

Uses fake Kanidm online backups; the full roundtrip with a real Kanidm runs in
tests/compose/test_idp.py.
"""

import gzip
import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from atlas.kernel.config import DatabaseSettings
from atlas.kernel.db.backup import (
    BackupError,
    check_idp_restore,
    collect_idp_backup,
    idp_restore_steps,
    kanidm_backup_time,
    newest_kanidm_backup,
    stage_idp_restore,
)

DOMAIN = "00000000-0000-4000-8000-00000000d0d0"
NOW = datetime(2026, 11, 4, 12, 0, 0, tzinfo=UTC)


def settings(backup_dir: Path, **extra: str) -> DatabaseSettings:
    return DatabaseSettings(
        _env_file=None,
        ATLAS_ENV="test",
        DATABASE_URL="postgresql+psycopg://atlas_app:Qm4tW9zLr2Xv@postgres:5432/atlas",
        ATLAS_BACKUP_DIR=str(backup_dir),
        **extra,
    )


BUNDLED = {"ATLAS_IDP_PUBLIC_URL": "https://id.example.com", "ATLAS_IDP_KANIDM_SERIES": "1.12"}


def kanidm_backup(online: Path, name: str, *, version: str = "1.12", domain: str = DOMAIN) -> Path:
    online.mkdir(parents=True, exist_ok=True)
    path = online / name
    body = {"version": version, "db_s_uuid": "s", "db_d_uuid": domain, "entries": []}
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump(body, fh)
    path.chmod(0o644)  # what Kanidm writes
    return path


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_kanidm_backup_names() -> None:
    assert kanidm_backup_time(Path("backup-2026-11-04T11:00:00Z.json.gz")) == datetime(
        2026, 11, 4, 11, tzinfo=UTC
    )
    nanos = kanidm_backup_time(Path("backup-2026-11-04T11:00:00.123456789Z.json.gz"))
    assert nanos is not None and nanos.microsecond == 123456
    for name in ("backup-2026-11-04T11:00:00Z.json", "kanidm.backup.json.gz", "x.json.gz"):
        assert kanidm_backup_time(Path(name)) is None


def test_newest_online_backup_wins(tmp_path: Path) -> None:
    online = tmp_path / "idp-online"
    kanidm_backup(online, "backup-2026-11-04T09:00:00Z.json.gz")
    newest = kanidm_backup(online, "backup-2026-11-04T11:00:00.5Z.json.gz")
    kanidm_backup(online, "backup-2026-11-04T10:00:00Z.json.gz")
    (online / "notes.txt").write_text("not a backup")
    assert newest_kanidm_backup(online) == newest


def test_collect_copies_the_newest_at_0600(tmp_path: Path) -> None:
    online = tmp_path / "idp-online"
    kanidm_backup(online, "backup-2026-11-04T09:00:00Z.json.gz", domain="old")
    newest = kanidm_backup(online, "backup-2026-11-04T11:30:00Z.json.gz")
    target = tmp_path / "set"
    target.mkdir()
    entry, warnings = collect_idp_backup(settings(tmp_path, **BUNDLED), target, NOW)
    assert warnings == []
    assert entry == {
        "file": "idp.json.gz",
        "source": newest.name,
        "created_at": "2026-11-04T11:30:00+00:00",
        "age_seconds": 1800,
        "kanidm_version": "1.12",
        "domain_uuid": DOMAIN,
    }
    copy = target / "idp.json.gz"
    assert copy.read_bytes() == newest.read_bytes()
    assert mode(copy) == 0o600


def test_collect_never_overwrites(tmp_path: Path) -> None:
    online = tmp_path / "idp-online"
    kanidm_backup(online, "backup-2026-11-04T11:30:00Z.json.gz")
    target = tmp_path / "set"
    target.mkdir()
    (target / "idp.json.gz").write_text("existing")
    with pytest.raises(FileExistsError):
        collect_idp_backup(settings(tmp_path, **BUNDLED), target, NOW)
    assert (target / "idp.json.gz").read_text() == "existing"


def test_collect_warns_when_kanidm_has_no_backup_yet(tmp_path: Path) -> None:
    (tmp_path / "idp-online").mkdir()
    target = tmp_path / "set"
    target.mkdir()
    entry, warnings = collect_idp_backup(settings(tmp_path, **BUNDLED), target, NOW)
    assert entry is None
    assert len(warnings) == 1 and "the IdP is NOT in this backup" in warnings[0]
    assert list(target.iterdir()) == []


def test_collect_warns_on_a_missing_dir_or_a_bad_file(tmp_path: Path) -> None:
    target = tmp_path / "set"
    target.mkdir()
    entry, warnings = collect_idp_backup(settings(tmp_path, **BUNDLED), target, NOW)
    assert entry is None and "cannot read" in warnings[0]
    online = tmp_path / "idp-online"
    online.mkdir()
    (online / "backup-2026-11-04T11:30:00Z.json.gz").write_bytes(b"not gzip")
    entry, warnings = collect_idp_backup(settings(tmp_path, **BUNDLED), target, NOW)
    assert entry is None and "not a readable Kanidm backup" in warnings[0]


def test_collect_warns_on_an_old_backup(tmp_path: Path) -> None:
    kanidm_backup(tmp_path / "idp-online", "backup-2026-10-30T12:00:00Z.json.gz")
    target = tmp_path / "set"
    target.mkdir()
    entry, warnings = collect_idp_backup(settings(tmp_path, **BUNDLED), target, NOW)
    assert entry is not None and entry["age_seconds"] == 5 * 24 * 3600
    assert "120 hours old" in warnings[0]


def backup_set(tmp_path: Path, idp: dict[str, object] | None) -> Path:
    source = tmp_path / "atlas-20261104T120000Z"
    source.mkdir()
    (source / "manifest.json").write_text(json.dumps({"format": 1, "idp": idp}))
    if idp:
        with gzip.open(source / "idp.json.gz", "wt") as fh:
            json.dump({"version": idp["kanidm_version"], "db_d_uuid": DOMAIN}, fh)
        (source / "idp.json.gz").chmod(0o600)
    return source


IDP_ENTRY = {"file": "idp.json.gz", "kanidm_version": "1.12", "domain_uuid": DOMAIN}


def test_restore_plan_same_version(tmp_path: Path) -> None:
    plan = check_idp_restore(settings(tmp_path, **BUNDLED), backup_set(tmp_path, IDP_ENTRY))
    assert plan.include and plan.warnings == ()


def test_restore_refuses_another_kanidm_version(tmp_path: Path) -> None:
    source = backup_set(tmp_path, {**IDP_ENTRY, "kanidm_version": "1.11"})
    with pytest.raises(BackupError, match=r"from Kanidm 1\.11, but this stack runs Kanidm 1\.12"):
        check_idp_restore(settings(tmp_path, **BUNDLED), source)


def test_restore_needs_the_running_series(tmp_path: Path) -> None:
    source = backup_set(tmp_path, IDP_ENTRY)
    with pytest.raises(BackupError, match="ATLAS_IDP_KANIDM_SERIES is not set"):
        check_idp_restore(settings(tmp_path, ATLAS_IDP_PUBLIC_URL="https://id.example.com"), source)


def test_restore_plan_without_idp_data_or_in_byo(tmp_path: Path) -> None:
    no_idp = backup_set(tmp_path, None)
    plan = check_idp_restore(settings(tmp_path, **BUNDLED), no_idp)
    assert not plan.include and "keeps its current data" in plan.warnings[0]
    assert check_idp_restore(settings(tmp_path), no_idp).warnings == ()
    with_idp = tmp_path / "other"
    with_idp.mkdir()
    byo = check_idp_restore(settings(tmp_path), backup_set(with_idp, IDP_ENTRY))
    assert not byo.include and "bundled IdP is off" in byo.warnings[0]


def test_restore_refuses_a_listed_but_missing_file(tmp_path: Path) -> None:
    source = backup_set(tmp_path, IDP_ENTRY)
    (source / "idp.json.gz").unlink()
    with pytest.raises(BackupError, match=r"idp\.json\.gz is missing"):
        check_idp_restore(settings(tmp_path, **BUNDLED), source)


def test_stage_for_the_idp_container(tmp_path: Path) -> None:
    source = backup_set(tmp_path, IDP_ENTRY)
    staging = tmp_path / "idp-restore"
    staging.mkdir()
    staged = stage_idp_restore(settings(tmp_path, **BUNDLED), source)
    # Kanidm decompresses only files named *.gz.
    assert staged == staging / "atlas-20261104T120000Z-idp.json.gz"
    assert staged.read_bytes() == (source / "idp.json.gz").read_bytes()
    assert mode(staged) == 0o640  # the idp group reads it; never world-readable
    # Re-running with the same backup reuses it; a different file is never overwritten.
    assert stage_idp_restore(settings(tmp_path, **BUNDLED), source) == staged
    staged.write_bytes(b"other")
    with pytest.raises(BackupError, match="exists with other content"):
        stage_idp_restore(settings(tmp_path, **BUNDLED), source)
    assert staged.read_bytes() == b"other"


def test_stage_needs_the_staging_dir(tmp_path: Path) -> None:
    with pytest.raises(BackupError, match="atlas-idp-init creates it"):
        stage_idp_restore(settings(tmp_path, **BUNDLED), backup_set(tmp_path, IDP_ENTRY))


def test_restore_steps() -> None:
    assert idp_restore_steps(Path("/var/backups/atlas/idp-restore/atlas-x-idp.json.gz")) == [
        "docker compose stop idp",
        "docker compose run --rm --no-deps idp /sbin/kanidmd database restore "
        "-c /etc/atlas-idp/server.toml /restore/atlas-x-idp.json.gz",
        "docker compose start idp",
    ]


def test_copies_ignore_a_permissive_umask(tmp_path: Path) -> None:
    kanidm_backup(tmp_path / "idp-online", "backup-2026-11-04T11:30:00Z.json.gz")
    target = tmp_path / "set"
    target.mkdir()
    old = os.umask(0)
    try:
        collect_idp_backup(settings(tmp_path, **BUNDLED), target, NOW)
    finally:
        os.umask(old)
    assert mode(target / "idp.json.gz") == 0o600
