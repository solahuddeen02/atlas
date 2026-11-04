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

import ipaddress
import os
import re
import string
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, TypeVar
from urllib.parse import unquote, urlsplit

from dotenv import dotenv_values
from pydantic import (
    Field,
    PrivateAttr,
    SecretStr,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
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

# Bundled IdP (ADR-001, PLAN Phase 5a).
# A private /28 outside Docker's default address pools (172.17-31.x, 192.168.x) and
# outside the ranges home routers and Podman usually take, so it rarely collides.
DEFAULT_IDP_FRONT_SUBNET = "10.249.73.0/28"
# Kanidm online backups: hourly, keeping a day of them. atlas-backup copies the newest
# one into each Atlas backup, so its age is at most an hour.
DEFAULT_IDP_BACKUP_SCHEDULE = "0 * * * *"
DEFAULT_IDP_BACKUP_VERSIONS = 24
IDP_KANIDM_SERIES = re.compile(r"^[0-9]+\.[0-9]+$")
_HOST_NAME = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$")
_CRON_FIELD = re.compile(r"^[0-9A-Za-z*/,?-]+$")


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


def check_idp_public_url(value: str, *, production: bool) -> str:
    """ATLAS_IDP_PUBLIC_URL: the bundled IdP's own HTTPS origin (ADR-001, hostname item 1)."""
    parts = urlsplit(value.strip())
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError("must be an absolute https:// URL (the IdP is only served over HTTPS)")
    if parts.username is not None or parts.password is not None:
        raise ValueError("must not contain a user name or password")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError("must be an origin with no path: the IdP needs its own hostname")
    host = parts.hostname.lower()
    if not _HOST_NAME.match(host):
        raise ValueError("has an invalid host name")
    if production and any(host == d or host.endswith("." + d) for d in PLACEHOLDER_DOMAINS):
        raise ValueError("still uses a placeholder domain (example.com); set your real URL")
    try:
        port = parts.port
    except ValueError:
        raise ValueError("has an invalid port") from None
    return f"https://{host}" + (f":{port}" if port not in (None, 443) else "")


def check_idp_front_subnet(value: str) -> str:
    """ATLAS_IDP_FRONT_SUBNET: the `idp-front` network (atlas-proxy + idp only)."""
    try:
        network = ipaddress.ip_network(value.strip(), strict=True)
    except ValueError:
        raise ValueError("must be an IPv4 network in CIDR form, e.g. 10.249.73.0/28") from None
    if network.version != 4:
        raise ValueError("must be an IPv4 network in CIDR form, e.g. 10.249.73.0/28")
    if not network.is_private or network.is_loopback or network.is_link_local:
        raise ValueError("must be a private range (RFC 1918), e.g. 10.249.73.0/28")
    if not 16 <= network.prefixlen <= 29:
        raise ValueError("must be between /16 and /29 (a /28 is plenty)")
    return str(network)


def check_idp_backup_schedule(value: str) -> str:
    """ATLAS_IDP_BACKUP_SCHEDULE: Kanidm's `[online_backup] schedule` (5 or 7 cron fields)."""
    fields = value.split()
    if len(fields) not in (5, 7) or not all(_CRON_FIELD.match(f) for f in fields):
        raise ValueError(
            "must be a cron schedule with 5 fields (minute hour day month weekday, "
            "e.g. '0 * * * *') or 7 (second ... year)"
        )
    return " ".join(fields)


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


class DatabaseSettings(BaseSettings):
    """What the database commands need (`migrate`, `backup`, `restore`), and no more."""

    model_config = SettingsConfigDict(
        extra="ignore",
        hide_input_in_errors=True,
        frozen=True,
    )

    # Declared first: the validators read it from `info.data`.
    env: Environment = Field(default=Environment.PRODUCTION, validation_alias="ATLAS_ENV")

    database_url: SecretStr = Field(validation_alias="DATABASE_URL")
    migrator_database_url_override: SecretStr | None = Field(
        default=None, validation_alias="ATLAS_MIGRATOR_DATABASE_URL"
    )

    # In-container path for backups and pre-migration dumps (ADR-005 §3, §4).
    backup_dir: Path = Field(
        default=Path("/var/backups/atlas"), validation_alias="ATLAS_BACKUP_DIR"
    )
    backup_keep_daily: int = Field(default=7, ge=1, validation_alias="ATLAS_BACKUP_KEEP_DAILY")
    backup_keep_weekly: int = Field(default=4, ge=0, validation_alias="ATLAS_BACKUP_KEEP_WEEKLY")

    # Bundled IdP (ADR-001). Set means the bundled Kanidm is in use; unset means BYO OIDC.
    idp_public_url: str | None = Field(default=None, validation_alias="ATLAS_IDP_PUBLIC_URL")
    # The Kanidm release series the stack runs (e.g. "1.12"). Set by docker-compose.yml
    # next to the pinned image; a backup only restores into the same series.
    idp_kanidm_series: str | None = Field(default=None, validation_alias="ATLAS_IDP_KANIDM_SERIES")

    @field_validator("idp_public_url", mode="before")
    @classmethod
    def _validate_idp_public_url(cls, value: object, info: ValidationInfo) -> object:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        if not isinstance(value, str):
            return value
        return check_idp_public_url(value, production=_is_production(info))

    @field_validator("idp_kanidm_series", mode="before")
    @classmethod
    def _validate_idp_kanidm_series(cls, value: object) -> object:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        if isinstance(value, str) and not IDP_KANIDM_SERIES.match(value.strip()):
            raise ValueError("must be a Kanidm release series such as 1.12")
        return value.strip() if isinstance(value, str) else value

    @property
    def bundled_idp(self) -> bool:
        return self.idp_public_url is not None

    @field_validator("database_url", "migrator_database_url_override")
    @classmethod
    def _validate_database_url(
        cls, value: SecretStr | None, info: ValidationInfo
    ) -> SecretStr | None:
        if value is not None:
            _check_database_url(value.get_secret_value(), production=_is_production(info))
        return value

    @property
    def is_production(self) -> bool:
        return self.env == Environment.PRODUCTION

    @property
    def migrator_database_url(self) -> SecretStr:
        """ADR-005: falls back to DATABASE_URL when no separate migrator URL is set."""
        return self.migrator_database_url_override or self.database_url


class DbInitSettings(DatabaseSettings):
    """`atlas-admin db-init`: the only command that uses the Postgres superuser."""

    postgres_superuser: str = Field(
        default="postgres", min_length=1, validation_alias="ATLAS_POSTGRES_SUPERUSER"
    )
    postgres_password_file: Path = Field(
        default=Path("/run/secrets/postgres/password"),
        validation_alias="ATLAS_POSTGRES_PASSWORD_FILE",
    )


class Settings(DatabaseSettings):
    """Kernel settings. Build with `load_settings()`, or `Settings(...)` in tests."""

    secret_key: SecretStr = Field(validation_alias="SECRET_KEY")
    secret_key_previous: SecretStr | None = Field(
        default=None, validation_alias="SECRET_KEY_PREVIOUS"
    )

    public_url: str = Field(validation_alias="ATLAS_PUBLIC_URL")

    # Generic OIDC relying-party settings: the same for any provider. With the bundled
    # IdP the issuer defaults from ATLAS_IDP_PUBLIC_URL (see `oidc_issuer`).
    # TODO(phase-5b): ATLAS_IDP_ADMIN_TOKEN, and the client secret written by the IdP
    # bootstrap. Until then ATLAS_OIDC_CLIENT_SECRET is set by hand, also with the
    # bundled IdP.
    oidc_issuer_setting: str | None = Field(default=None, validation_alias="ATLAS_OIDC_ISSUER")
    oidc_client_id: str = Field(
        default="atlas",
        min_length=1,
        pattern=r"^[A-Za-z0-9_-]+$",
        validation_alias="ATLAS_OIDC_CLIENT_ID",
    )
    oidc_client_secret: SecretStr = Field(validation_alias="ATLAS_OIDC_CLIENT_SECRET")
    identity_admin_setting: Literal["kanidm", "manual"] | None = Field(
        default=None, validation_alias="ATLAS_IDENTITY_ADMIN"
    )

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

    @field_validator("public_url", "oidc_issuer_setting")
    @classmethod
    def _validate_url(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        return _check_url(value, production=_is_production(info))

    @model_validator(mode="after")
    def _check_identity(self) -> Settings:
        if self.idp_public_url is not None and _host(self.idp_public_url) == _host(self.public_url):
            raise ValueError(
                "ATLAS_IDP_PUBLIC_URL: must use a different host name than ATLAS_PUBLIC_URL "
                "(the IdP needs its own origin)"
            )
        if self.oidc_issuer_setting is None and self.idp_public_url is None:
            raise ValueError(
                "ATLAS_OIDC_ISSUER: is required unless ATLAS_IDP_PUBLIC_URL is set "
                "(the bundled IdP)"
            )
        return self

    @property
    def oidc_issuer(self) -> str:
        """ATLAS_OIDC_ISSUER, or the bundled Kanidm's issuer for this client."""
        if self.oidc_issuer_setting is not None:
            return self.oidc_issuer_setting
        assert self.idp_public_url is not None  # guaranteed by _check_identity
        return f"{self.idp_public_url}/oauth2/openid/{self.oidc_client_id}"

    @property
    def identity_admin(self) -> Literal["kanidm", "manual"]:
        """Who manages identities: the bundled Kanidm, or the operator by hand (BYO)."""
        if self.identity_admin_setting is not None:
            return self.identity_admin_setting
        return "kanidm" if self.bundled_idp else "manual"

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
    """Settings for `atlas-admin init`, which runs before any secret exists.

    The IdP settings matter only with the bundled IdP (ATLAS_IDP_PUBLIC_URL set): then
    init also writes the internal CA, the IdP's certificate and its server.toml.
    """

    model_config = SettingsConfigDict(extra="ignore", frozen=True, hide_input_in_errors=True)

    env: Environment = Field(default=Environment.PRODUCTION, validation_alias="ATLAS_ENV")
    secrets_path: Path = Field(default=Path("./secrets"), validation_alias="ATLAS_SECRETS_DIR")
    public_url: str | None = Field(default=None, validation_alias="ATLAS_PUBLIC_URL")
    idp_public_url: str | None = Field(default=None, validation_alias="ATLAS_IDP_PUBLIC_URL")
    idp_front_subnet: str = Field(
        default=DEFAULT_IDP_FRONT_SUBNET, validation_alias="ATLAS_IDP_FRONT_SUBNET"
    )
    idp_backup_schedule: str = Field(
        default=DEFAULT_IDP_BACKUP_SCHEDULE, validation_alias="ATLAS_IDP_BACKUP_SCHEDULE"
    )
    idp_backup_versions: int = Field(
        default=DEFAULT_IDP_BACKUP_VERSIONS,
        ge=1,
        le=1000,
        validation_alias="ATLAS_IDP_BACKUP_VERSIONS",
    )

    @field_validator("public_url", "idp_public_url", mode="before")
    @classmethod
    def _empty_is_unset(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("idp_public_url")
    @classmethod
    def _validate_idp_public_url(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        return check_idp_public_url(value, production=_is_production(info))

    @field_validator("idp_front_subnet", mode="before")
    @classmethod
    def _validate_subnet(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return DEFAULT_IDP_FRONT_SUBNET
        return check_idp_front_subnet(value) if isinstance(value, str) else value

    @field_validator("idp_backup_schedule", mode="before")
    @classmethod
    def _validate_schedule(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return DEFAULT_IDP_BACKUP_SCHEDULE
        return check_idp_backup_schedule(value) if isinstance(value, str) else value

    @model_validator(mode="after")
    def _idp_has_its_own_host(self) -> InitSettings:
        if (
            self.idp_public_url is not None
            and self.public_url is not None
            and _host(self.idp_public_url) == _host(self.public_url)
        ):
            raise ValueError(
                "ATLAS_IDP_PUBLIC_URL: must use a different host name than ATLAS_PUBLIC_URL "
                "(the IdP needs its own origin)"
            )
        return self

    @property
    def bundled_idp(self) -> bool:
        return self.idp_public_url is not None


_M = TypeVar("_M", bound=BaseSettings)
_D = TypeVar("_D", bound=DatabaseSettings)


def _field_alias(model: type[BaseSettings], loc: tuple[int | str, ...]) -> str:
    if not loc:
        return "configuration"
    name = str(loc[0])
    field = model.model_fields.get(name)
    alias = field.validation_alias if field is not None else None
    return alias if isinstance(alias, str) else name.upper()


def _problems(model: type[BaseSettings], error: ValidationError) -> list[str]:
    problems = []
    for item in error.errors(include_input=False, include_url=False):
        reason = "is required" if item["type"] == "missing" else item["msg"]
        reason = reason.removeprefix("Value error, ")
        if not item["loc"]:
            # A rule across several settings: its message names them itself.
            problems.append(reason)
            continue
        problems.append(f"{_field_alias(model, item['loc'])}: {reason}")
    return problems


def _build(model: type[_M], **values: Any) -> _M:
    try:
        return model(**values)
    except ValidationError as exc:
        raise ConfigError(_problems(model, exc)) from None


def build_settings(**values: Any) -> Settings:
    """Construct `Settings`, converting validation errors into a value-free `ConfigError`."""
    return _build(Settings, **values)


def _env_files(environ: Mapping[str, str]) -> list[Path]:
    raw = environ.get("ATLAS_ENV_FILES", DEFAULT_ENV_FILES)
    return [Path(p.strip()) for p in raw.split(",") if p.strip()]


def _load(model: type[_D]) -> tuple[_D, list[Path]]:
    """Build `model` for a running process from the env files and the environment."""
    files = _env_files(os.environ)
    missing = [str(p) for p in files if not p.is_file()]
    if missing:
        raise ConfigError(
            [f"ATLAS_ENV_FILES: file not found: {p} (run `atlas-admin init`)" for p in missing]
        )
    settings = _build(model, _env_file=tuple(files))
    if settings.env == Environment.TEST:
        raise ConfigError(["ATLAS_ENV: 'test' is only for Settings built inside tests"])
    return settings, files


def load_settings() -> Settings:
    """Build `Settings` for the API process."""
    settings, files = _load(Settings)
    connector_secrets: dict[str, str] = {}
    for path in files:
        for key, value in dotenv_values(path).items():
            if key.upper().startswith(CONNECTOR_PREFIX) and value:
                connector_secrets[key.upper()] = value
    for key, value in os.environ.items():
        if key.upper().startswith(CONNECTOR_PREFIX) and value:
            connector_secrets[key.upper()] = value
    settings._connector_secrets = connector_secrets
    return settings


def load_database_settings() -> DatabaseSettings:
    """Build `DatabaseSettings` for `migrate`, `backup` and `restore`."""
    return _load(DatabaseSettings)[0]


def load_db_init_settings() -> DbInitSettings:
    return _load(DbInitSettings)[0]


def load_init_settings() -> InitSettings:
    return _build(InitSettings)
