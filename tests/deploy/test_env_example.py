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


def test_idp_settings_deferred_until_spike() -> None:
    # Bundled-IdP variables stay commented out until Spike S (PLAN-v1, Phase 2).
    v = values()
    for name in ("ATLAS_IDP_PUBLIC_URL", "COMPOSE_PROFILES", "ATLAS_IDP_VOLUME"):
        assert name not in v
    assert "TODO(spike-s)" in ENV_EXAMPLE.read_text()
