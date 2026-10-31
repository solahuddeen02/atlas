"""`atlas-admin db-init`: create the Atlas roles, database and schema (ADR-005 §1).

Runs as the Postgres superuser, after Postgres is healthy and before
`atlas-admin migrate`. It's the only command that uses the superuser password;
the API and migrations never see it.

Idempotent: roles and the database are created if missing. A role's password is
changed only when the one in app.env no longer works, which is how a rotated
password (`atlas-admin init` after deleting the line) gets applied.
Passwords are quoted by psycopg and never printed or logged.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import psycopg
from psycopg import sql
from sqlalchemy.engine import URL

from atlas.kernel.config import DbInitSettings
from atlas.kernel.db.metadata import SCHEMA
from atlas.kernel.db.postgres import APP_ROLE, MIGRATOR_ROLE, parse_url


class DbInitError(RuntimeError):
    pass


@dataclass
class DbInitReport:
    lines: list[str] = field(default_factory=list)

    def add(self, line: str) -> None:
        self.lines.append(line)


def _connect(url: URL, *, user: str, password: str, dbname: str) -> psycopg.Connection[object]:
    return psycopg.connect(
        host=url.host or "localhost",
        port=url.port or 5432,
        user=user,
        password=password,
        dbname=dbname,
        autocommit=True,
        connect_timeout=10,
        options="-c timezone=UTC",
    )


def _can_login(url: URL, role: str, password: str, dbname: str) -> bool:
    try:
        with _connect(url, user=role, password=password, dbname=dbname):
            return True
    except psycopg.OperationalError:
        return False


def _role_urls(settings: DbInitSettings) -> tuple[URL, URL]:
    if settings.migrator_database_url_override is None:
        raise DbInitError("ATLAS_MIGRATOR_DATABASE_URL: is required for db-init")
    app = parse_url(settings.database_url)
    migrator = parse_url(settings.migrator_database_url_override)
    for name, url, role in (
        ("DATABASE_URL", app, APP_ROLE),
        ("ATLAS_MIGRATOR_DATABASE_URL", migrator, MIGRATOR_ROLE),
    ):
        if url.username != role:
            raise DbInitError(f"{name}: must connect as {role}")
        if not url.password:
            raise DbInitError(f"{name}: must include a password")
    if (app.host, app.port, app.database) != (migrator.host, migrator.port, migrator.database):
        raise DbInitError(
            "DATABASE_URL and ATLAS_MIGRATOR_DATABASE_URL must point at the same database"
        )
    return app, migrator


def _superuser_password(settings: DbInitSettings) -> str:
    path = settings.postgres_password_file
    try:
        password = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise DbInitError(
            f"ATLAS_POSTGRES_PASSWORD_FILE: cannot read {path}: {exc.strerror}"
        ) from None
    if not password:
        raise DbInitError(f"ATLAS_POSTGRES_PASSWORD_FILE: {path} is empty")
    return password


def run_db_init(settings: DbInitSettings) -> DbInitReport:
    app, migrator = _role_urls(settings)
    database = app.database or ""
    if not database:
        raise DbInitError("DATABASE_URL: must name a database")
    superuser = settings.postgres_superuser
    super_password = _superuser_password(settings)
    report = DbInitReport()

    try:
        admin = _connect(app, user=superuser, password=super_password, dbname="postgres")
    except psycopg.OperationalError as exc:
        raise DbInitError(f"cannot connect to Postgres as {superuser}: {exc}") from None

    with admin:
        for role, url in ((MIGRATOR_ROLE, migrator), (APP_ROLE, app)):
            password = url.password or ""
            exists = admin.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
            attrs = sql.SQL("LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION")
            if exists is None:
                admin.execute(
                    sql.SQL("CREATE ROLE {} {} PASSWORD {}").format(
                        sql.Identifier(role), attrs, sql.Literal(password)
                    )
                )
                report.add(f"created role {role}")
                continue
            admin.execute(sql.SQL("ALTER ROLE {} {}").format(sql.Identifier(role), attrs))
            if _can_login(app, role, password, "postgres"):
                report.add(f"kept role {role}")
            else:
                admin.execute(
                    sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                        sql.Identifier(role), sql.Literal(password)
                    )
                )
                report.add(f"updated password for role {role}")

        for role in (MIGRATOR_ROLE, APP_ROLE):
            admin.execute(
                sql.SQL("ALTER ROLE {} SET timezone = 'UTC'").format(sql.Identifier(role))
            )

        exists = admin.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (database,)
        ).fetchone()
        db = sql.Identifier(database)
        if exists is None:
            admin.execute(
                sql.SQL("CREATE DATABASE {} OWNER {} ENCODING 'UTF8'").format(
                    db, sql.Identifier(MIGRATOR_ROLE)
                )
            )
            report.add(f"created database {database}")
        else:
            admin.execute(
                sql.SQL("ALTER DATABASE {} OWNER TO {}").format(db, sql.Identifier(MIGRATOR_ROLE))
            )
            report.add(f"kept database {database}")
        admin.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(db))
        admin.execute(
            sql.SQL("GRANT CONNECT, TEMPORARY ON DATABASE {} TO {}, {}").format(
                db, sql.Identifier(MIGRATOR_ROLE), sql.Identifier(APP_ROLE)
            )
        )

    with _connect(app, user=superuser, password=super_password, dbname=database) as conn:
        schema = sql.Identifier(SCHEMA)
        migrator_role = sql.Identifier(MIGRATOR_ROLE)
        conn.execute(
            sql.SQL("CREATE SCHEMA IF NOT EXISTS {} AUTHORIZATION {}").format(schema, migrator_role)
        )
        conn.execute(sql.SQL("ALTER SCHEMA {} OWNER TO {}").format(schema, migrator_role))
        conn.execute(
            sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(schema, sql.Identifier(APP_ROLE))
        )
        # Nobody but the migrator creates objects, anywhere (ADR-005 §1, "Roles").
        conn.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
        report.add(f"schema {SCHEMA} owned by {MIGRATOR_ROLE}; public schema locked down")

    return report
