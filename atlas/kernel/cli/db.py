"""`atlas-admin db-init | migrate | backup | restore` (ADR-005)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from atlas.kernel.config import (
    ConfigError,
    load_database_settings,
    load_db_init_settings,
)

# Exit codes: 1 = the operation failed; 2 = configuration is invalid.


def _config_error(command: str, exc: ConfigError) -> int:
    print(f"atlas-admin {command}: {exc}", file=sys.stderr)
    return 2


def cmd_db_init(args: argparse.Namespace) -> int:
    from atlas.kernel.db.bootstrap import DbInitError, run_db_init

    try:
        settings = load_db_init_settings()
        report = run_db_init(settings)
    except ConfigError as exc:
        return _config_error("db-init", exc)
    except DbInitError as exc:
        print(f"atlas-admin db-init: {exc}", file=sys.stderr)
        return 1
    for line in report.lines:
        print(line)
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    from atlas.kernel.db.migrate import MigrateError, run_migrate

    try:
        settings = load_database_settings()
        result = run_migrate(settings)
    except ConfigError as exc:
        return _config_error("migrate", exc)
    except MigrateError as exc:
        print(f"atlas-admin migrate: {exc}", file=sys.stderr)
        return 1
    if result.dump is not None:
        print(f"pre-migration backup: {result.dump}")
    before = ", ".join(result.before) or "(empty)"
    after = ", ".join(result.after)
    print(f"migrated {before} -> {after}" if result.changed else f"up to date at {after}")
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    from atlas.kernel.db.backup import BackupError, run_backup
    from atlas.kernel.db.postgres import PgToolError

    try:
        settings = load_database_settings()
        result = run_backup(settings)
    except ConfigError as exc:
        return _config_error("backup", exc)
    except (BackupError, PgToolError) as exc:
        print(f"atlas-admin backup: {exc}", file=sys.stderr)
        return 1
    print(f"backup written: {result.path}")
    for path in result.pruned:
        print(f"pruned: {path}")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    from atlas.kernel.db.backup import BackupError, run_restore
    from atlas.kernel.db.postgres import PgToolError

    if not args.yes:
        print(
            "atlas-admin restore: this replaces the current database contents. "
            "Stop atlas-api first, then re-run with --yes.",
            file=sys.stderr,
        )
        return 1
    try:
        settings = load_database_settings()
        revisions = run_restore(settings, args.source)
    except ConfigError as exc:
        return _config_error("restore", exc)
    except (BackupError, PgToolError) as exc:
        print(f"atlas-admin restore: {exc}", file=sys.stderr)
        return 1
    print(f"restored {args.source} at revision {', '.join(revisions)}")
    return 0


def add_commands(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    db_init = commands.add_parser(
        "db-init",
        help="Create or update the Atlas database roles, database and schema (superuser).",
    )
    db_init.set_defaults(handler=cmd_db_init)

    migrate = commands.add_parser(
        "migrate", help="Back up if needed, then apply pending migrations and seed."
    )
    migrate.set_defaults(handler=cmd_migrate)

    backup = commands.add_parser("backup", help="Write a database backup and apply retention.")
    backup.set_defaults(handler=cmd_backup)

    restore = commands.add_parser(
        "restore", help="Restore a backup directory. Stop atlas-api first."
    )
    restore.add_argument(
        "--from", dest="source", type=Path, required=True, help="Backup directory."
    )
    restore.add_argument("--yes", action="store_true", help="Confirm replacing current data.")
    restore.set_defaults(handler=cmd_restore)
