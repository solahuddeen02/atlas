"""`atlas-admin`: operator commands (ARCHITECTURE §2, ADR-004)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from atlas.kernel.cli import db
from atlas.kernel.cli.init import InitError, run_init
from atlas.kernel.config import load_init_settings


def _cmd_init(args: argparse.Namespace) -> int:
    secrets_dir = args.secrets_dir or load_init_settings().secrets_path
    try:
        report = run_init(Path(secrets_dir))
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
    init.set_defaults(handler=_cmd_init)

    db.add_commands(commands)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    code: int = args.handler(args)
    return code
