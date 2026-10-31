"""`atlas-admin init`: generate missing secrets (ADR-004 §3).

Rules:
- Directories are created `0700` and files `0600`.
- An existing file is never rewritten. If an env file lacks a key (or its value is
  empty), only that key is appended; every existing line stays as it was. This is
  what makes "delete the line and re-run" rotate a secret (ADR-004 §6).
- Values come from `secrets` (the OS CSPRNG) and are never printed.
"""

from __future__ import annotations

import os
import secrets
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

DB_HOST = "postgres"
DB_PORT = 5432
DB_NAME = "atlas"
APP_ROLE = "atlas_app"
MIGRATOR_ROLE = "atlas_migrator"

DIR_MODE = 0o700
FILE_MODE = 0o600


class InitError(RuntimeError):
    pass


def _token() -> str:
    return secrets.token_urlsafe(32)


def _database_url(role: str) -> str:
    return f"postgresql+psycopg://{role}:{_token()}@{DB_HOST}:{DB_PORT}/{DB_NAME}"


# Keys generated into atlas/app.env, in file order.
APP_ENV_GENERATORS: dict[str, Callable[[], str]] = {
    "SECRET_KEY": _token,
    "DATABASE_URL": lambda: _database_url(APP_ROLE),
    "ATLAS_MIGRATOR_DATABASE_URL": lambda: _database_url(MIGRATOR_ROLE),
}
# TODO(spike-s): bundled IdP only, after its bootstrap: ATLAS_OIDC_CLIENT_SECRET and
# ATLAS_IDP_ADMIN_TOKEN in atlas/app.env, and the idp/ directory (internal CA, its key,
# server certificate and key). Not generated until Spike S confirms the IdP.
# The atlas_app/atlas_migrator roles are created from these passwords by
# `atlas-admin db-init` (atlas.kernel.db.bootstrap) once Postgres is up.


@dataclass(frozen=True)
class Ownership:
    """Who each secrets subdirectory belongs to inside the containers (ADR-004 §3).

    - `atlas/` and the backups dir: the Atlas image's user (uid/gid 10001), 0700/0600.
    - `postgres/`: the Postgres image's user (uid 999), with the Atlas group allowed to
      read (0750/0640), because `atlas-db-init` runs as the Atlas user and needs the
      superuser password. Containers only see the subdirectories they mount, so the
      API, which never mounts `postgres/`, can't read it anyway.
    """

    app_uid: int = 10001
    app_gid: int = 10001
    postgres_uid: int = 999


@dataclass
class InitReport:
    created: list[str] = field(default_factory=list)
    appended: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = [f"created {item}" for item in self.created]
        out += [f"added {item}" for item in self.appended]
        out += [f"kept {item}" for item in self.kept]
        out += [f"warning: {item}" for item in self.warnings]
        return out


def _ensure_dir(path: Path, root: Path, report: InitReport) -> None:
    label = f"{path.relative_to(root)}/" if path != root else f"{path}/"
    if path.exists():
        if not path.is_dir():
            raise InitError(f"{path} exists and is not a directory")
        # Group read is deliberate for postgres/ (see Ownership); group write or any
        # access for others is not.
        if stat.S_IMODE(path.stat().st_mode) & 0o027:
            report.warnings.append(f"{label} is accessible by group/others; consider chmod 700")
        return
    path.mkdir(mode=DIR_MODE, parents=True)
    os.chmod(path, DIR_MODE)  # mkdir's mode is filtered by the umask
    report.created.append(label)


def _write_new_file(path: Path, content: str) -> None:
    """Create `path` with mode 0600. Fails instead of replacing a file that appeared meanwhile."""
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.link(tmp, path)  # raises FileExistsError rather than overwriting
    finally:
        tmp.unlink(missing_ok=True)


def _env_lines(values: dict[str, str]) -> str:
    return "".join(f"{key}={value}\n" for key, value in values.items())


def _ensure_env_file(
    path: Path,
    root: Path,
    generators: dict[str, Callable[[], str]],
    report: InitReport,
    header: str,
) -> None:
    label = str(path.relative_to(root))
    if not path.exists():
        _write_new_file(path, header + _env_lines({k: gen() for k, gen in generators.items()}))
        keys = ", ".join(generators)
        report.created.append(f"{label}" + (f" ({keys})" if keys else ""))
        return

    if not path.is_file():
        raise InitError(f"{path} exists and is not a regular file")
    existing = dotenv_values(path)
    missing = {k: gen for k, gen in generators.items() if not existing.get(k)}
    if not missing:
        report.kept.append(label)
        return

    text = path.read_text(encoding="utf-8")
    prefix = "" if text == "" or text.endswith("\n") else "\n"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(prefix + _env_lines({k: gen() for k, gen in missing.items()}))
        fh.flush()
        os.fsync(fh.fileno())
    report.appended.append(f"{', '.join(missing)} to {label}")


def _ensure_secret_file(path: Path, root: Path, report: InitReport) -> None:
    label = str(path.relative_to(root))
    if path.exists():
        report.kept.append(label)
        return
    _write_new_file(path, _token() + "\n")
    report.created.append(label)


APP_ENV_HEADER = (
    "# Generated by `atlas-admin init`. Secrets: never commit or share this file.\n"
    "# Delete a line and re-run `atlas-admin init` to regenerate that value.\n"
)
CONNECTORS_ENV_HEADER = (
    "# Connector secrets referenced from the admin console, e.g.\n"
    "# ATLAS_CONNECTOR_IMMICH_API_KEY=...\n"
)


def _set_owner(path: Path, uid: int, gid: int, mode: int) -> None:
    try:
        os.chown(path, uid, gid)
    except PermissionError:
        raise InitError(f"cannot change the owner of {path}; --chown needs root") from None
    os.chmod(path, mode)


def _apply_ownership(root: Path, backup_dir: Path | None, owners: Ownership) -> None:
    app = (owners.app_uid, owners.app_gid)
    atlas_dir = root / "atlas"
    _set_owner(atlas_dir, *app, DIR_MODE)
    for child in atlas_dir.iterdir():
        if child.is_file() and not child.name.startswith("."):
            _set_owner(child, *app, FILE_MODE)

    postgres_dir = root / "postgres"
    _set_owner(postgres_dir, owners.postgres_uid, owners.app_gid, 0o750)
    _set_owner(postgres_dir / "password", owners.postgres_uid, owners.app_gid, 0o640)

    if backup_dir is not None:
        _set_owner(backup_dir, *app, DIR_MODE)


def run_init(
    secrets_dir: Path,
    *,
    backup_dir: Path | None = None,
    owners: Ownership | None = None,
) -> InitReport:
    """Generate missing secrets. With `owners`, also set the container ownership."""
    root = secrets_dir
    report = InitReport()
    _ensure_dir(root, root, report)

    atlas_dir = root / "atlas"
    _ensure_dir(atlas_dir, root, report)
    _ensure_env_file(atlas_dir / "app.env", root, APP_ENV_GENERATORS, report, APP_ENV_HEADER)
    _ensure_env_file(atlas_dir / "connectors.env", root, {}, report, CONNECTORS_ENV_HEADER)

    postgres_dir = root / "postgres"
    _ensure_dir(postgres_dir, root, report)
    _ensure_secret_file(postgres_dir / "password", root, report)

    if backup_dir is not None and not backup_dir.exists():
        backup_dir.mkdir(mode=DIR_MODE, parents=True)
        os.chmod(backup_dir, DIR_MODE)
        report.created.append(f"{backup_dir}/")
    if owners is not None:
        _apply_ownership(root, backup_dir, owners)
    return report
