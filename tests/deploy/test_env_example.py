import re
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values

ENV_EXAMPLE = Path(__file__).resolve().parents[2] / "deploy" / ".env.example"
SECRET_NAME = re.compile(r"(?i)(secret|password|token|key)")
# Names that match SECRET_NAME but hold no secret.
NOT_SECRET = {"ATLAS_SECRETS_DIR"}


def values() -> dict[str, str | None]:
    return dotenv_values(ENV_EXAMPLE)


def test_no_secrets() -> None:
    for name, value in values().items():
        if name not in NOT_SECRET:
            assert not SECRET_NAME.search(name), f"{name} looks secret; it belongs in secrets/"
        assert value is not None


def test_only_placeholder_domains() -> None:
    for name, value in values().items():
        if value and "://" in value:
            host = urlsplit(value).hostname or ""
            assert host == "example.com" or host.endswith(".example.com"), name


def test_generic_safe_defaults() -> None:
    v = values()
    assert v["ATLAS_BIND_ADDRESS"] == "127.0.0.1"
    assert v["ATLAS_HTTP_PORT"] == "8080"
    assert v["TZ"] == "UTC"
    assert v["ATLAS_SECRETS_DIR"] == "./secrets"
    assert v["ATLAS_BACKUP_DIR"] == "./backups"


def test_bundled_idp_is_on_by_default() -> None:
    # ADR-001: the default install bundles Kanidm (PLAN Phase 5a).
    from atlas.kernel.config import (
        DEFAULT_IDP_BACKUP_SCHEDULE,
        DEFAULT_IDP_FRONT_SUBNET,
        check_idp_backup_schedule,
        check_idp_front_subnet,
    )

    v = values()
    assert v["COMPOSE_PROFILES"] == "bundled-idp"
    assert v["ATLAS_IDP_PUBLIC_URL"] == "https://id.example.com"
    assert (
        urlsplit(v["ATLAS_IDP_PUBLIC_URL"] or "").hostname
        != urlsplit(v["ATLAS_PUBLIC_URL"] or "").hostname
    )
    assert v["ATLAS_IDP_VOLUME"] == "atlas_idp"
    # The documented private default, the same as the code's and the compose file's.
    assert v["ATLAS_IDP_FRONT_SUBNET"] == DEFAULT_IDP_FRONT_SUBNET
    assert check_idp_front_subnet(v["ATLAS_IDP_FRONT_SUBNET"] or "") == DEFAULT_IDP_FRONT_SUBNET
    assert v["ATLAS_IDP_BACKUP_SCHEDULE"] == DEFAULT_IDP_BACKUP_SCHEDULE
    assert check_idp_backup_schedule(v["ATLAS_IDP_BACKUP_SCHEDULE"] or "")
    compose = (ENV_EXAMPLE.parent / "docker-compose.yml").read_text()
    assert f"${{ATLAS_IDP_FRONT_SUBNET:-{DEFAULT_IDP_FRONT_SUBNET}}}" in compose


def test_no_kanidm_variables() -> None:
    # Kanidm's config lives only in server.toml: KANIDM_* would override it.
    assert not [name for name in values() if name.upper().startswith("KANIDM_")]
