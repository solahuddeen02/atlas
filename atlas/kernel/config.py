"""Typed, fail-closed configuration (ADR-004).

This is the only module that reads the process environment or env files
(enforced by ruff's banned-api rule). Everything else receives a `Settings`.

Sources, highest priority first:
1. real environment variables;
2. the env files listed in `ATLAS_ENV_FILES` (written by `atlas-admin init`);
3. code defaults. No secret has a default.

Error messages name the variable and the rule. They never contain the value.
"""

from __future__ import annotations

import os
import string
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urlsplit

from dotenv import dotenv_values
from pydantic import Field, PrivateAttr, SecretStr, ValidationError, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    PRODUCTION = "production"
    DEVELOPMENT = "development"
    TEST = "test"


DEFAULT_ENV_FILES = "/run/secrets/atlas/app.env,/run/secrets/atlas/connectors.env"
CONNECTOR_PREFIX = "ATLAS_CONNECTOR_"

MIN_SECRET_KEY_LENGTH = 43  # 32 random bytes, base64url-encoded
MIN_SECRET_KEY_DISTINCT_CHARS = 16
MIN_HEX_SECRET_KEY_LENGTH = 64  # 32 random bytes, hex-encoded
# Hex has only 16 symbols, and about 1 in 4 random 64-char hex keys misses one.
MIN_HEX_SECRET_KEY_DISTINCT_CHARS = 12

# Every placeholder ever committed to this repo, plus common weak values.
DENYLISTED_SECRETS = frozenset(
    {
        "dev-secret-change-in-production",
        "change-this-in-production",
        "changeme",
        "secret",
        "password",
    }
)
DENYLISTED_DB_PASSWORDS = DENYLISTED_SECRETS | {"atlas", "postgres"}
PLACEHOLDER_DOMAINS = ("example.com", "example.org", "example.net")
DATABASE_SCHEME = "postgresql+psycopg"

GENERATE_HINT = "run `atlas-admin init`, or generate one with `openssl rand -base64 32`"


class ConfigError(RuntimeError):
    """Invalid configuration. Each problem is `NAME: reason`, with no values."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("Invalid configuration:\n" + "\n".join(f"  - {p}" for p in problems))


def _is_placeholder(value: str) -> bool:
    lowered = value.lower()
    return lowered in DENYLISTED_SECRETS or ("change" in lowered and "production" in lowered)


def _check_secret_key(value: str) -> None:
    if _is_placeholder(value):
        raise ValueError(f"is a known placeholder and must not be used; {GENERATE_HINT}")
    is_hex = all(c in string.hexdigits for c in value)
    min_length = MIN_HEX_SECRET_KEY_LENGTH if is_hex else MIN_SECRET_KEY_LENGTH
    min_distinct = MIN_HEX_SECRET_KEY_DISTINCT_CHARS if is_hex else MIN_SECRET_KEY_DISTINCT_CHARS
    if len(value) < min_length:
        kind = "hex " if is_hex else ""
        raise ValueError(f"is too short (minimum {min_length} {kind}characters); {GENERATE_HINT}")
    if len(set(value)) < min_distinct:
        raise ValueError(
            f"is too repetitive (minimum {min_distinct} distinct characters); {GENERATE_HINT}"
        )


def _is_production(info: ValidationInfo) -> bool:
    # If ATLAS_ENV itself is invalid it is missing from info.data: apply the strict rules.
    env: object = info.data.get("env", Environment.PRODUCTION)
    return env == Environment.PRODUCTION


def _check_url(value: str, *, production: bool) -> str:
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("must be an absolute http(s) URL")
    if production:
        if parts.scheme != "https":
            raise ValueError("must use https:// in production")
        host = parts.hostname.lower()
        if any(host == d or host.endswith("." + d) for d in PLACEHOLDER_DOMAINS):
            raise ValueError("still uses a placeholder domain (example.com); set your real URL")
    return value.rstrip("/")


def _check_database_url(value: str, *, production: bool) -> None:
    parts = urlsplit(value)
    if parts.scheme != DATABASE_SCHEME:
        raise ValueError(f"must start with {DATABASE_SCHEME}://")
    if not parts.hostname:
        raise ValueError("must include a host")
    if production:
        password = unquote(parts.password or "")
        if not password:
            raise ValueError("must include a password in production")
        if password.lower() in DENYLISTED_DB_PASSWORDS or _is_placeholder(password):
            raise ValueError("uses a well-known password; run `atlas-admin init` to generate one")


class Settings(BaseSettings):
    """Kernel settings. Build with `load_settings()`, or `Settings(...)` in tests."""

    model_config = SettingsConfigDict(
        extra="ignore",
        hide_input_in_errors=True,
        frozen=True,
    )

    # Declared first: the validators below read it from `info.data`.
    env: Environment = Field(default=Environment.PRODUCTION, validation_alias="ATLAS_ENV")

    secret_key: SecretStr = Field(validation_alias="SECRET_KEY")
    secret_key_previous: SecretStr | None = Field(
        default=None, validation_alias="SECRET_KEY_PREVIOUS"
    )

    database_url: SecretStr = Field(validation_alias="DATABASE_URL")
    migrator_database_url_override: SecretStr | None = Field(
        default=None, validation_alias="ATLAS_MIGRATOR_DATABASE_URL"
    )

    public_url: str = Field(validation_alias="ATLAS_PUBLIC_URL")

    # Generic OIDC relying-party settings: the same for any provider.
    # TODO(spike-s): with the bundled IdP, default the issuer from ATLAS_IDP_PUBLIC_URL
    # (path confirmed by the spike), and add ATLAS_IDP_PUBLIC_URL, ATLAS_IDP_ADMIN_TOKEN
    # and ATLAS_IDENTITY_ADMIN=kanidm.
    oidc_issuer: str = Field(validation_alias="ATLAS_OIDC_ISSUER")
    oidc_client_id: str = Field(
        default="atlas", min_length=1, validation_alias="ATLAS_OIDC_CLIENT_ID"
    )
    oidc_client_secret: SecretStr = Field(validation_alias="ATLAS_OIDC_CLIENT_SECRET")

    log_level: Literal["debug", "info", "warning", "error"] = Field(
        default="info", validation_alias="ATLAS_LOG_LEVEL"
    )
    log_format: Literal["json", "text"] = Field(default="json", validation_alias="ATLAS_LOG_FORMAT")
    dev_cors_origins_raw: str = Field(default="", validation_alias="ATLAS_DEV_CORS_ORIGINS")

    @field_validator("secret_key", "secret_key_previous")
    @classmethod
    def _validate_secret_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            _check_secret_key(value.get_secret_value())
        return value

    @field_validator("database_url", "migrator_database_url_override")
    @classmethod
    def _validate_database_url(
        cls, value: SecretStr | None, info: ValidationInfo
    ) -> SecretStr | None:
        if value is not None:
            _check_database_url(value.get_secret_value(), production=_is_production(info))
        return value

    @field_validator("public_url", "oidc_issuer")
    @classmethod
    def _validate_url(cls, value: str, info: ValidationInfo) -> str:
        return _check_url(value, production=_is_production(info))

    @field_validator("oidc_client_secret")
    @classmethod
    def _validate_client_secret(cls, value: SecretStr) -> SecretStr:
        secret = value.get_secret_value()
        if not secret:
            raise ValueError("is required")
        if _is_placeholder(secret):
            raise ValueError("is a known placeholder and must not be used")
        return value

    # Derived settings. Production values can't be overridden.

    @property
    def is_production(self) -> bool:
        return self.env == Environment.PRODUCTION

    @property
    def migrator_database_url(self) -> SecretStr:
        """ADR-005: falls back to DATABASE_URL when no separate migrator URL is set."""
        return self.migrator_database_url_override or self.database_url

    @property
    def docs_enabled(self) -> bool:
        return not self.is_production

    @property
    def cookie_secure(self) -> bool:
        return self.is_production

    @property
    def cors_origins(self) -> tuple[str, ...]:
        if self.is_production:
            return ()
        return tuple(o.strip() for o in self.dev_cors_origins_raw.split(",") if o.strip())

    def connector_secret(self, ref: str) -> SecretStr | None:
        """The value of a connector secret reference (`ATLAS_CONNECTOR_*`), if set."""
        value = self._connector_secrets.get(ref.upper())
        return SecretStr(value) if value else None

    def connector_secret_status(self, ref: str) -> Literal["set", "missing"]:
        return "set" if self.connector_secret(ref) is not None else "missing"

    # Filled in by load_settings(); empty for Settings built directly.
    _connector_secrets: dict[str, str] = PrivateAttr(default_factory=dict)


class InitSettings(BaseSettings):
    """Settings for `atlas-admin init`, which runs before any secret exists."""

    model_config = SettingsConfigDict(extra="ignore", frozen=True)

    secrets_path: Path = Field(default=Path("./secrets"), validation_alias="ATLAS_SECRETS_DIR")


def _field_alias(loc: tuple[int | str, ...]) -> str:
    if not loc:
        return "configuration"
    name = str(loc[0])
    field = Settings.model_fields.get(name)
    alias = field.validation_alias if field is not None else None
    return alias if isinstance(alias, str) else name.upper()


def _problems(error: ValidationError) -> list[str]:
    problems = []
    for item in error.errors(include_input=False, include_url=False):
        reason = "is required" if item["type"] == "missing" else item["msg"]
        reason = reason.removeprefix("Value error, ")
        problems.append(f"{_field_alias(item['loc'])}: {reason}")
    return problems


def build_settings(**values: Any) -> Settings:
    """Construct `Settings`, converting validation errors into a value-free `ConfigError`."""
    try:
        return Settings(**values)
    except ValidationError as exc:
        raise ConfigError(_problems(exc)) from None


def _env_files(environ: Mapping[str, str]) -> list[Path]:
    raw = environ.get("ATLAS_ENV_FILES", DEFAULT_ENV_FILES)
    return [Path(p.strip()) for p in raw.split(",") if p.strip()]


def load_settings() -> Settings:
    """Build `Settings` for a running process from env files and the environment."""
    environ = os.environ
    files = _env_files(environ)
    missing = [str(p) for p in files if not p.is_file()]
    if missing:
        raise ConfigError(
            [f"ATLAS_ENV_FILES: file not found: {p} (run `atlas-admin init`)" for p in missing]
        )

    settings = build_settings(_env_file=tuple(files))
    if settings.env == Environment.TEST:
        raise ConfigError(["ATLAS_ENV: 'test' is only for Settings built inside tests"])

    connector_secrets: dict[str, str] = {}
    for path in files:
        for key, value in dotenv_values(path).items():
            if key.upper().startswith(CONNECTOR_PREFIX) and value:
                connector_secrets[key.upper()] = value
    for key, value in environ.items():
        if key.upper().startswith(CONNECTOR_PREFIX) and value:
            connector_secrets[key.upper()] = value
    settings._connector_secrets = connector_secrets
    return settings


def load_init_settings() -> InitSettings:
    return InitSettings()
