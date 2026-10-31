import re
from collections.abc import Callable
from pathlib import Path

import pytest

from atlas.kernel.config import (
    ConfigError,
    Environment,
    Settings,
    build_settings,
    load_settings,
)

MakeSettings = Callable[..., Settings]


def problems_for(make: MakeSettings, **overrides: str | None) -> str:
    with pytest.raises(ConfigError) as exc:
        make(**overrides)
    return str(exc.value)


# SECRET_KEY


@pytest.mark.parametrize(
    "bad_key",
    [
        "dev-secret-change-in-production",
        "change-this-in-production",
        "changeme",
        "secret",
        "password",
        "PLEASE-Change-Me-Before-Production-0123456789abcdefghijklmnop",
    ],
)
def test_secret_key_denylisted(make_settings: MakeSettings, bad_key: str) -> None:
    message = problems_for(make_settings, SECRET_KEY=bad_key)
    assert "SECRET_KEY: is a known placeholder" in message
    assert bad_key not in message


def test_secret_key_missing(make_settings: MakeSettings) -> None:
    assert "SECRET_KEY: is required" in problems_for(make_settings, SECRET_KEY=None)


def test_secret_key_short(make_settings: MakeSettings) -> None:
    key = "Ab3$Cd4%Ef5^Gh6&Ij7*Kl8(Mn9)"
    message = problems_for(make_settings, SECRET_KEY=key)
    assert "SECRET_KEY: is too short (minimum 43" in message
    assert key not in message


def test_secret_key_low_variety(make_settings: MakeSettings) -> None:
    key = "abcabcabcabcabcabcabcabcabcabcabcabcabcabcabcxyz"
    message = problems_for(make_settings, SECRET_KEY=key)
    assert "SECRET_KEY: is too repetitive" in message
    assert key not in message


def test_secret_key_hex_needs_64_chars(make_settings: MakeSettings) -> None:
    short_hex = "0123456789abcdef" * 3  # 48 hex chars, enough if it were base64url
    assert "minimum 64 hex characters" in problems_for(make_settings, SECRET_KEY=short_hex)
    long_hex = "0123456789abcdef" * 4
    make_settings(SECRET_KEY=long_hex)


def test_secret_key_previous_checked_too(make_settings: MakeSettings) -> None:
    assert "SECRET_KEY_PREVIOUS: is a known placeholder" in problems_for(
        make_settings, SECRET_KEY_PREVIOUS="changeme"
    )


def test_generated_style_key_accepted(make_settings: MakeSettings) -> None:
    import secrets

    make_settings(SECRET_KEY=secrets.token_urlsafe(32))
    make_settings(SECRET_KEY=secrets.token_hex(32))


# Values never leak


def test_no_value_in_errors_or_repr(make_settings: MakeSettings) -> None:
    secret_db = "postgresql+psycopg://atlas_app:postgres@db/atlas"
    message = problems_for(
        make_settings,
        ATLAS_ENV="production",
        SECRET_KEY="changeme",
        DATABASE_URL=secret_db,
        ATLAS_OIDC_CLIENT_SECRET="changeme",
    )
    assert "changeme" not in message
    assert secret_db not in message
    assert ":postgres@" not in message

    settings = make_settings()
    text = repr(settings) + str(settings)
    for name in ("SECRET_KEY", "DATABASE_URL", "ATLAS_OIDC_CLIENT_SECRET"):
        from tests.conftest import VALID

        assert VALID[name] not in text


def test_all_problems_reported_at_once(make_settings: MakeSettings) -> None:
    message = problems_for(
        make_settings, SECRET_KEY=None, DATABASE_URL=None, ATLAS_OIDC_CLIENT_SECRET=None
    )
    for name in ("SECRET_KEY", "DATABASE_URL", "ATLAS_OIDC_CLIENT_SECRET"):
        assert f"{name}: is required" in message


# Production is the default and is strict


def test_production_is_default(make_settings: MakeSettings) -> None:
    settings = make_settings(
        ATLAS_ENV=None,
        ATLAS_PUBLIC_URL="https://atlas.home.arpa",
    )
    assert settings.env is Environment.PRODUCTION
    assert settings.is_production


def test_invalid_env_value_gets_strict_rules(make_settings: MakeSettings) -> None:
    message = problems_for(make_settings, ATLAS_ENV="prod", ATLAS_PUBLIC_URL="http://x.test")
    assert "ATLAS_ENV:" in message
    assert "ATLAS_PUBLIC_URL: must use https://" in message


@pytest.mark.parametrize("name", ["ATLAS_PUBLIC_URL", "ATLAS_OIDC_ISSUER"])
@pytest.mark.parametrize(
    "url",
    ["https://atlas.example.com", "https://id.example.org/issuer", "https://example.net"],
)
def test_placeholder_domain_rejected_in_production(
    make_settings: MakeSettings, name: str, url: str
) -> None:
    message = problems_for(make_settings, ATLAS_ENV="production", **{name: url})
    assert f"{name}: still uses a placeholder domain" in message


def test_http_rejected_in_production(make_settings: MakeSettings) -> None:
    message = problems_for(make_settings, ATLAS_ENV="production", ATLAS_PUBLIC_URL="http://a.b")
    assert "ATLAS_PUBLIC_URL: must use https:// in production" in message


def test_development_allows_http_and_example_com(make_settings: MakeSettings) -> None:
    settings = make_settings(
        ATLAS_ENV="development",
        ATLAS_PUBLIC_URL="http://localhost:5173/",
        ATLAS_OIDC_ISSUER="https://id.example.com",
    )
    assert settings.public_url == "http://localhost:5173"


def test_url_must_be_absolute(make_settings: MakeSettings) -> None:
    message = problems_for(make_settings, ATLAS_ENV="development", ATLAS_PUBLIC_URL="atlas.lan")
    assert "ATLAS_PUBLIC_URL: must be an absolute http(s) URL" in message


# Database URLs


@pytest.mark.parametrize(
    "url", ["sqlite:///./atlas.db", "postgresql://u:Pw9x2@db/atlas", "postgres://u:p@db/a"]
)
def test_database_url_scheme(make_settings: MakeSettings, url: str) -> None:
    message = problems_for(make_settings, ATLAS_ENV="development", DATABASE_URL=url)
    assert "DATABASE_URL: must start with postgresql+psycopg://" in message


@pytest.mark.parametrize("password", ["atlas", "postgres", "password", "changeme", "Postgres", ""])
def test_database_password_denylisted_in_production(
    make_settings: MakeSettings, password: str
) -> None:
    url = f"postgresql+psycopg://atlas_app:{password}@postgres:5432/atlas"
    message = problems_for(make_settings, ATLAS_ENV="production", DATABASE_URL=url)
    assert "DATABASE_URL:" in message


def test_migrator_url_validated_and_falls_back(make_settings: MakeSettings) -> None:
    settings = make_settings()
    assert settings.migrator_database_url == settings.database_url
    assert "ATLAS_MIGRATOR_DATABASE_URL: must start with" in problems_for(
        make_settings, ATLAS_MIGRATOR_DATABASE_URL="sqlite:///x.db"
    )


# OIDC


def test_oidc_client_secret_required(make_settings: MakeSettings) -> None:
    assert "ATLAS_OIDC_CLIENT_SECRET: is required" in problems_for(
        make_settings, ATLAS_OIDC_CLIENT_SECRET=None
    )
    assert "ATLAS_OIDC_CLIENT_SECRET: is required" in problems_for(
        make_settings, ATLAS_OIDC_CLIENT_SECRET=""
    )


def test_oidc_client_secret_not_placeholder(make_settings: MakeSettings) -> None:
    assert "ATLAS_OIDC_CLIENT_SECRET: is a known placeholder" in problems_for(
        make_settings, ATLAS_OIDC_CLIENT_SECRET="secret"
    )


def test_oidc_client_id_defaults(make_settings: MakeSettings) -> None:
    assert make_settings().oidc_client_id == "atlas"


# Derived production rules


def test_production_forces_docs_cors_and_cookies(make_settings: MakeSettings) -> None:
    prod = make_settings(ATLAS_ENV="production", ATLAS_DEV_CORS_ORIGINS="http://localhost:5173")
    assert not prod.docs_enabled
    assert prod.cors_origins == ()
    assert prod.cookie_secure

    dev = make_settings(
        ATLAS_ENV="development", ATLAS_DEV_CORS_ORIGINS="http://localhost:5173, http://a.test"
    )
    assert dev.docs_enabled
    assert dev.cors_origins == ("http://localhost:5173", "http://a.test")
    assert not dev.cookie_secure


def test_settings_are_immutable(make_settings: MakeSettings) -> None:
    settings = make_settings()
    with pytest.raises(Exception):  # noqa: B017 (pydantic raises ValidationError)
        settings.env = Environment.DEVELOPMENT  # type: ignore[misc]


# load_settings(): env files and precedence


def write_env(path: Path, values: dict[str, str]) -> Path:
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items()))
    return path


@pytest.fixture
def env_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    from tests.conftest import VALID

    app = write_env(tmp_path / "app.env", {k: v for k, v in VALID.items() if k != "ATLAS_ENV"})
    connectors = write_env(tmp_path / "connectors.env", {"ATLAS_CONNECTOR_IMMICH_API_KEY": "k1"})
    monkeypatch.setenv("ATLAS_ENV_FILES", f"{app},{connectors}")
    monkeypatch.setenv("ATLAS_ENV", "development")
    return app, connectors


def test_load_settings_from_files(env_files: tuple[Path, Path]) -> None:
    from tests.conftest import VALID

    settings = load_settings()
    assert settings.secret_key.get_secret_value() == VALID["SECRET_KEY"]
    assert settings.env is Environment.DEVELOPMENT


def test_env_overrides_file(env_files: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    import secrets

    override = secrets.token_urlsafe(32)
    monkeypatch.setenv("SECRET_KEY", override)
    assert load_settings().secret_key.get_secret_value() == override


def test_env_override_is_validated_too(
    env_files: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SECRET_KEY", "changeme")
    with pytest.raises(ConfigError, match="SECRET_KEY: is a known placeholder"):
        load_settings()


def test_missing_env_file_is_an_error(
    tmp_path: Path, env_files: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "nope.env"
    monkeypatch.setenv("ATLAS_ENV_FILES", f"{env_files[0]},{missing}")
    with pytest.raises(ConfigError, match="ATLAS_ENV_FILES: file not found"):
        load_settings()


def test_default_env_files_are_the_container_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ConfigError, match=re.escape("/run/secrets/atlas/app.env")):
        load_settings()


def test_load_settings_refuses_test_mode(
    env_files: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ATLAS_ENV", "test")
    with pytest.raises(ConfigError, match="ATLAS_ENV: 'test' is only for Settings built inside"):
        load_settings()


# Connector secret references


def test_connector_secret_status(
    env_files: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ATLAS_CONNECTOR_OTHER_TOKEN", "from-env")
    settings = load_settings()
    assert settings.connector_secret_status("ATLAS_CONNECTOR_IMMICH_API_KEY") == "set"
    assert settings.connector_secret_status("atlas_connector_other_token") == "set"
    assert settings.connector_secret_status("ATLAS_CONNECTOR_MISSING_KEY") == "missing"
    secret = settings.connector_secret("ATLAS_CONNECTOR_IMMICH_API_KEY")
    assert secret is not None
    assert secret.get_secret_value() == "k1"
    assert "k1" not in repr(settings)


def test_connector_secrets_empty_when_built_directly() -> None:
    from tests.conftest import VALID

    settings = build_settings(_env_file=None, **VALID)
    assert settings.connector_secret_status("ATLAS_CONNECTOR_IMMICH_API_KEY") == "missing"


# Database command settings (Phase 3)


def test_database_settings_need_no_api_secrets() -> None:
    from atlas.kernel.config import DatabaseSettings

    settings = DatabaseSettings(
        _env_file=None,
        DATABASE_URL="postgresql+psycopg://atlas_app:Qm4tW9zLr2Xv@postgres:5432/atlas",
    )
    assert settings.backup_dir == Path("/var/backups/atlas")
    assert (settings.backup_keep_daily, settings.backup_keep_weekly) == (7, 4)
    assert settings.migrator_database_url == settings.database_url


def test_backup_retention_must_be_positive(make_settings: MakeSettings) -> None:
    assert "ATLAS_BACKUP_KEEP_DAILY:" in problems_for(make_settings, ATLAS_BACKUP_KEEP_DAILY="0")


def test_db_init_settings_defaults() -> None:
    from atlas.kernel.config import DbInitSettings

    settings = DbInitSettings(
        _env_file=None,
        DATABASE_URL="postgresql+psycopg://atlas_app:Qm4tW9zLr2Xv@postgres:5432/atlas",
    )
    assert settings.postgres_superuser == "postgres"
    assert settings.postgres_password_file == Path("/run/secrets/postgres/password")
