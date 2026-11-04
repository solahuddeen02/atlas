"""`atlas-admin`: operator commands (ARCHITECTURE §2, ADR-004)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from atlas.kernel.cli import db
from atlas.kernel.cli.idp import IdpOptions
from atlas.kernel.cli.init import InitError, Ownership, run_init
from atlas.kernel.config import ConfigError, load_init_settings


def _cmd_init(args: argparse.Namespace) -> int:
    try:
        settings = load_init_settings()
    except ConfigError as exc:
        print(f"atlas-admin init: {exc}", file=sys.stderr)
        return 2
    secrets_dir = args.secrets_dir or settings.secrets_path
    owners = (
        Ownership(
            app_uid=args.app_uid,
            app_gid=args.app_gid,
            postgres_uid=args.postgres_uid,
            idp_uid=args.idp_uid,
            idp_gid=args.idp_gid,
        )
        if args.chown
        else None
    )
    idp = None
    if settings.idp_public_url is not None:
        idp = IdpOptions(
            public_url=settings.idp_public_url,
            front_subnet=settings.idp_front_subnet,
            backup_schedule=settings.idp_backup_schedule,
            backup_versions=settings.idp_backup_versions,
            data_dir=args.idp_data_dir,
            rotate_cert=args.rotate_idp_cert,
        )
    elif args.rotate_idp_cert or args.idp_data_dir is not None:
        print(
            "atlas-admin init: ATLAS_IDP_PUBLIC_URL is not set, but the bundled IdP needs it "
            "(set it in deploy/.env, or remove bundled-idp from COMPOSE_PROFILES for BYO OIDC)",
            file=sys.stderr,
        )
        return 2
    try:
        report = run_init(Path(secrets_dir), backup_dir=args.backup_dir, owners=owners, idp=idp)
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
    init.add_argument("--idp-uid", type=int, default=defaults.idp_uid)
    init.add_argument("--idp-gid", type=int, default=defaults.idp_gid)
    init.add_argument(
        "--idp-data-dir",
        type=Path,
        help="Bundled IdP: the idp data volume, owned by the idp user (with --chown).",
    )
    init.add_argument(
        "--rotate-idp-cert",
        action="store_true",
        help="Bundled IdP: issue a new idp TLS key and certificate from the existing CA.",
    )
    init.set_defaults(handler=_cmd_init)

    db.add_commands(commands)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    code: int = args.handler(args)
    return code
