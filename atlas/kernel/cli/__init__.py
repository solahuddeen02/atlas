"""`atlas-admin`: operator commands (ARCHITECTURE §2, ADR-004)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from atlas.kernel.cli import db
from atlas.kernel.cli.init import InitError, Ownership, run_init
from atlas.kernel.config import load_init_settings


def _cmd_init(args: argparse.Namespace) -> int:
    secrets_dir = args.secrets_dir or load_init_settings().secrets_path
    owners = (
        Ownership(app_uid=args.app_uid, app_gid=args.app_gid, postgres_uid=args.postgres_uid)
        if args.chown
        else None
    )
    try:
        report = run_init(Path(secrets_dir), backup_dir=args.backup_dir, owners=owners)
    except InitError as exc:
        print(f"atlas-admin init: {exc}", file=sys.stderr)
        return 1
    for line in report.lines():
        print(line)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="atlas-admin")
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser(
        "init",
        help="Generate missing secrets. Never overwrites an existing value.",
        description=(
            "Create missing secret files under the secrets directory "
            "(ATLAS_SECRETS_DIR, default ./secrets). Existing files and values are kept."
        ),
    )
    init.add_argument("--secrets-dir", type=Path, help="Overrides ATLAS_SECRETS_DIR.")
    init.add_argument(
        "--backup-dir", type=Path, help="Also create this backups directory (and own it)."
    )
    init.add_argument(
        "--chown",
        action="store_true",
        help="Set container ownership on the secrets (and backups) dirs. Needs root.",
    )
    defaults = Ownership()
    init.add_argument("--app-uid", type=int, default=defaults.app_uid)
    init.add_argument("--app-gid", type=int, default=defaults.app_gid)
    init.add_argument("--postgres-uid", type=int, default=defaults.postgres_uid)
    init.set_defaults(handler=_cmd_init)

    db.add_commands(commands)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    code: int = args.handler(args)
    return code
