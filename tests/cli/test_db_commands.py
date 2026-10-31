from pathlib import Path

import pytest

from atlas.kernel.cli import main
from tests.conftest import PgTestDb


def write_env(path: Path, values: dict[str, str]) -> Path:
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items()))
    return path


@pytest.mark.parametrize("command", ["db-init", "migrate", "backup"])
def test_bad_config_exits_2(
    command: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    env = write_env(tmp_path / "app.env", {"DATABASE_URL": "sqlite:///x.db"})
    monkeypatch.setenv("ATLAS_ENV_FILES", str(env))
    assert main([command]) == 2
    assert "DATABASE_URL: must start with postgresql+psycopg://" in capsys.readouterr().err


def test_restore_requires_yes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["restore", "--from", str(tmp_path)]) == 1
    assert "re-run with --yes" in capsys.readouterr().err


def test_migrate_needs_only_database_settings(
    migrated_db: PgTestDb,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # No SECRET_KEY, public URL or OIDC settings: migrate doesn't need them.
    settings = migrated_db.settings
    env = write_env(
        tmp_path / "app.env",
        {
            "DATABASE_URL": settings.database_url.get_secret_value(),
            "ATLAS_MIGRATOR_DATABASE_URL": settings.migrator_database_url.get_secret_value(),
            "ATLAS_BACKUP_DIR": str(settings.backup_dir),
        },
    )
    monkeypatch.setenv("ATLAS_ENV_FILES", str(env))
    monkeypatch.setenv("ATLAS_ENV", "development")
    assert main(["migrate"]) == 0
    out = capsys.readouterr()
    assert "up to date at 0001" in out.out
    for secret in (migrated_db.cluster.app_password, migrated_db.cluster.migrator_password):
        assert secret not in out.out + out.err

    assert main(["backup"]) == 0
    assert "backup written:" in capsys.readouterr().out
