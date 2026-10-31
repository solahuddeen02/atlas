import os
import secrets
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import sql
from sqlalchemy.engine import make_url

from atlas.kernel.config import DatabaseSettings, DbInitSettings, Settings, build_settings

# Variables Settings/InitSettings read. Cleared for every test so a developer's or CI's
# environment can never leak into a test.
_CONFIG_PREFIXES = ("ATLAS_",)
_CONFIG_NAMES = ("SECRET_KEY", "SECRET_KEY_PREVIOUS", "DATABASE_URL")
# TEST_POSTGRES_URL is read once at import (below), before any test runs.

STRONG_KEY = "k3Y-" + "".join(chr(c) for c in range(ord("A"), ord("Z") + 1)) + "abcdefghijklmnop"
VALID: dict[str, str] = {
    "ATLAS_ENV": "test",
    "SECRET_KEY": STRONG_KEY,
    "DATABASE_URL": "postgresql+psycopg://atlas_app:Qm4tW9zLr2Xv@postgres:5432/atlas",
    "ATLAS_PUBLIC_URL": "https://atlas.home.arpa",
    "ATLAS_OIDC_ISSUER": "https://id.home.arpa/oauth2/openid/atlas",
    "ATLAS_OIDC_CLIENT_SECRET": "oidc-client-Zr8pQ2vN6",
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith(_CONFIG_PREFIXES) or name in _CONFIG_NAMES:
            monkeypatch.delenv(name)


@pytest.fixture
def make_settings() -> Callable[..., Settings]:
    """Valid test Settings; pass overrides by variable name, None to drop one."""

    def make(**overrides: Any) -> Settings:
        values: dict[str, Any] = {**VALID, **overrides}
        values = {k: v for k, v in values.items() if v is not None}
        return build_settings(_env_file=None, **values)

    return make


# PostgreSQL fixtures (ADR-005). TEST_POSTGRES_URL is a superuser URL, e.g.
#   postgresql://postgres:<password>@127.0.0.1:5432/postgres
# Without it the DB tests are skipped locally; in CI (CI=true) they fail instead.
TEST_POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL", "")
IN_CI = os.environ.get("CI", "").lower() == "true"


@dataclass
class PgCluster:
    host: str
    port: int
    superuser: str
    password_file: Path
    app_password: str
    migrator_password: str

    def url(self, role: str, password: str, database: str) -> str:
        return f"postgresql+psycopg://{role}:{password}@{self.host}:{self.port}/{database}"


@dataclass
class PgTestDb:
    name: str
    cluster: PgCluster
    settings: DatabaseSettings
    db_init: DbInitSettings

    def superuser_connect(self) -> psycopg.Connection[Any]:
        return psycopg.connect(
            host=self.cluster.host,
            port=self.cluster.port,
            user=self.cluster.superuser,
            password=self.cluster.password_file.read_text().strip(),
            dbname=self.name,
            autocommit=True,
        )


@pytest.fixture(scope="session")
def pg_cluster(tmp_path_factory: pytest.TempPathFactory) -> PgCluster:
    if not TEST_POSTGRES_URL:
        if IN_CI:
            pytest.fail("TEST_POSTGRES_URL must be set in CI")
        pytest.skip("TEST_POSTGRES_URL not set (see CONTRIBUTING: database tests)")
    url = make_url(TEST_POSTGRES_URL)
    password_file = tmp_path_factory.mktemp("pg") / "password"
    password_file.write_text(url.password or "")
    return PgCluster(
        host=url.host or "127.0.0.1",
        port=url.port or 5432,
        superuser=url.username or "postgres",
        password_file=password_file,
        app_password="App" + secrets.token_urlsafe(24),
        migrator_password="Mig" + secrets.token_urlsafe(24),
    )


def _database_settings(cluster: PgCluster, name: str, backup_dir: Path) -> dict[str, str]:
    return {
        "ATLAS_ENV": "development",
        "DATABASE_URL": cluster.url("atlas_app", cluster.app_password, name),
        "ATLAS_MIGRATOR_DATABASE_URL": cluster.url(
            "atlas_migrator", cluster.migrator_password, name
        ),
        "ATLAS_BACKUP_DIR": str(backup_dir),
    }


@pytest.fixture
def empty_db(pg_cluster: PgCluster, tmp_path: Path) -> Iterator[PgTestDb]:
    """A fresh database after `atlas-admin db-init`, before any migration."""
    from atlas.kernel.db.bootstrap import run_db_init

    name = f"atlas_test_{secrets.token_hex(6)}"
    values = _database_settings(pg_cluster, name, tmp_path / "backups")
    db_init = DbInitSettings(
        _env_file=None,
        ATLAS_POSTGRES_PASSWORD_FILE=str(pg_cluster.password_file),
        ATLAS_POSTGRES_SUPERUSER=pg_cluster.superuser,
        **values,
    )
    run_db_init(db_init)
    db = PgTestDb(
        name=name,
        cluster=pg_cluster,
        settings=DatabaseSettings(_env_file=None, **values),
        db_init=db_init,
    )
    yield db
    with psycopg.connect(
        host=pg_cluster.host,
        port=pg_cluster.port,
        user=pg_cluster.superuser,
        password=pg_cluster.password_file.read_text().strip(),
        dbname="postgres",
        autocommit=True,
    ) as admin:
        admin.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
        )


@pytest.fixture
def migrated_db(empty_db: PgTestDb) -> PgTestDb:
    from atlas.kernel.db.migrate import run_migrate

    run_migrate(empty_db.settings)
    return empty_db


# Compose tests (tests/compose) need a Docker daemon. They run only when
# RUN_COMPOSE_TESTS=1 (the `compose` CI job sets it); everywhere else they're skipped.
RUN_COMPOSE_TESTS = os.environ.get("RUN_COMPOSE_TESTS", "") == "1"
