import stat
from pathlib import Path

import pytest
from dotenv import dotenv_values

from atlas.kernel.cli import main
from atlas.kernel.cli.init import run_init
from atlas.kernel.config import build_settings
from tests.conftest import VALID

APP_KEYS = ("SECRET_KEY", "DATABASE_URL", "ATLAS_MIGRATOR_DATABASE_URL")
FILES = ("atlas/app.env", "atlas/connectors.env", "postgres/password")
DIRS = ("", "atlas", "postgres")


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def snapshot(root: Path) -> dict[str, tuple[bytes, int, int]]:
    return {
        str(p.relative_to(root)): (
            p.read_bytes() if p.is_file() else b"",
            p.stat().st_mtime_ns,
            mode(p),
        )
        for p in sorted(root.rglob("*"))
    }


@pytest.fixture
def secrets_dir(tmp_path: Path) -> Path:
    return tmp_path / "secrets"


def test_creates_everything_with_strict_modes(secrets_dir: Path) -> None:
    run_init(secrets_dir)
    for d in DIRS:
        assert mode(secrets_dir / d) == 0o700, d
    for f in FILES:
        assert mode(secrets_dir / f) == 0o600, f
    assert sorted(str(p.relative_to(secrets_dir)) for p in secrets_dir.rglob("*")) == sorted(
        ["atlas", "postgres", *FILES]
    )


def test_strict_modes_despite_permissive_umask(secrets_dir: Path) -> None:
    import os

    old = os.umask(0)
    try:
        run_init(secrets_dir)
    finally:
        os.umask(old)
    assert mode(secrets_dir / "atlas") == 0o700
    assert mode(secrets_dir / "atlas/app.env") == 0o600


def test_idp_secrets_not_generated_before_spike(secrets_dir: Path) -> None:
    # Deferred until Spike S confirms the bundled IdP (PLAN-v1, Phase 2).
    run_init(secrets_dir)
    assert not (secrets_dir / "idp").exists()
    app_env = dotenv_values(secrets_dir / "atlas/app.env")
    assert "ATLAS_IDP_ADMIN_TOKEN" not in app_env
    assert "ATLAS_OIDC_CLIENT_SECRET" not in app_env


def test_generated_values_pass_settings_validation(secrets_dir: Path) -> None:
    run_init(secrets_dir)
    app_env = dotenv_values(secrets_dir / "atlas/app.env")
    assert set(APP_KEYS) <= set(app_env)
    settings = build_settings(
        _env_file=None,
        **{**VALID, "ATLAS_ENV": "production", **{k: v for k, v in app_env.items() if v}},
    )
    assert settings.database_url.get_secret_value().startswith("postgresql+psycopg://atlas_app:")
    assert settings.migrator_database_url.get_secret_value().startswith(
        "postgresql+psycopg://atlas_migrator:"
    )
    assert len((secrets_dir / "postgres/password").read_text().strip()) >= 43


def test_second_run_changes_nothing(secrets_dir: Path) -> None:
    run_init(secrets_dir)
    before = snapshot(secrets_dir)
    report = run_init(secrets_dir)
    assert snapshot(secrets_dir) == before
    assert not report.created
    assert not report.appended


def test_operator_files_untouched(secrets_dir: Path) -> None:
    (secrets_dir / "atlas").mkdir(parents=True)
    (secrets_dir / "postgres").mkdir()
    app_env = secrets_dir / "atlas/app.env"
    app_env.write_text(
        "# mine\nSECRET_KEY=operator-key\nDATABASE_URL=operator-db\n"
        "ATLAS_MIGRATOR_DATABASE_URL=operator-migrator\n"
    )
    (secrets_dir / "atlas/connectors.env").write_text("ATLAS_CONNECTOR_X_KEY=x\n")
    (secrets_dir / "postgres/password").write_text("operator-pg")
    before = snapshot(secrets_dir)

    run_init(secrets_dir)
    assert snapshot(secrets_dir) == before


def test_only_missing_keys_are_appended(secrets_dir: Path) -> None:
    run_init(secrets_dir)
    app_env = secrets_dir / "atlas/app.env"
    original = dotenv_values(app_env)
    # Rotation per ADR-004 §6: delete the SECRET_KEY line, re-run init.
    kept_lines = [
        line for line in app_env.read_text().splitlines(True) if not line.startswith("SECRET_KEY=")
    ]
    app_env.write_text("".join(kept_lines))

    report = run_init(secrets_dir)
    assert report.appended == ["SECRET_KEY to atlas/app.env"]
    text = app_env.read_text()
    assert text.startswith("".join(kept_lines))
    after = dotenv_values(app_env)
    assert after["DATABASE_URL"] == original["DATABASE_URL"]
    assert after["SECRET_KEY"] != original["SECRET_KEY"]


def test_empty_value_counts_as_missing(secrets_dir: Path) -> None:
    run_init(secrets_dir)
    app_env = secrets_dir / "atlas/app.env"
    app_env.write_text(app_env.read_text() + "SECRET_KEY=\n")
    run_init(secrets_dir)
    assert dotenv_values(app_env)["SECRET_KEY"]


def test_appends_on_a_new_line(secrets_dir: Path) -> None:
    (secrets_dir / "atlas").mkdir(parents=True)
    app_env = secrets_dir / "atlas/app.env"
    app_env.write_text("SECRET_KEY=operator-key")  # no trailing newline
    run_init(secrets_dir)
    values = dotenv_values(app_env)
    assert values["SECRET_KEY"] == "operator-key"
    assert values["DATABASE_URL"]


def test_values_are_random(tmp_path: Path) -> None:
    run_init(tmp_path / "a")
    run_init(tmp_path / "b")
    a = dotenv_values(tmp_path / "a/atlas/app.env")
    b = dotenv_values(tmp_path / "b/atlas/app.env")
    for key in APP_KEYS:
        assert a[key] != b[key]
    assert (tmp_path / "a/postgres/password").read_text() != (
        tmp_path / "b/postgres/password"
    ).read_text()


def test_warns_on_loose_existing_dir(secrets_dir: Path) -> None:
    secrets_dir.mkdir(mode=0o755)
    secrets_dir.chmod(0o755)
    report = run_init(secrets_dir)
    assert any("accessible by group/others" in w for w in report.warnings)
    assert mode(secrets_dir) == 0o755  # the operator's directory is not changed


def test_cli_prints_no_secret_values(secrets_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["init", "--secrets-dir", str(secrets_dir)]) == 0
    out = capsys.readouterr()
    values = [v for v in dotenv_values(secrets_dir / "atlas/app.env").values() if v]
    values.append((secrets_dir / "postgres/password").read_text().strip())
    for value in values:
        assert value not in out.out + out.err
    assert "created atlas/app.env" in out.out


def test_cli_uses_atlas_secrets_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "from-env"
    monkeypatch.setenv("ATLAS_SECRETS_DIR", str(target))
    assert main(["init"]) == 0
    assert (target / "atlas/app.env").is_file()


def test_cli_reports_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    not_a_dir = tmp_path / "file"
    not_a_dir.write_text("")
    assert main(["init", "--secrets-dir", str(not_a_dir)]) == 1
    assert "is not a directory" in capsys.readouterr().err
