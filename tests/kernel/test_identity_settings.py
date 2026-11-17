"""Settings of the IdentityAdmin adapter (PLAN Phase 5b-3b).

`ATLAS_IDP_ADMIN_TOKEN` is required with the bundled IdP; where atlas-api reaches Kanidm,
the CA it trusts and the group UUID file have defaults that docker-compose.yml matches.
Only atlas/kernel/config.py reads the environment (ADR-004).
"""

from pathlib import Path

import pytest

from atlas.kernel.config import ConfigError, build_settings
from tests.conftest import VALID

IDP = "https://id.home.arpa"
TOKEN_VALUE = "console-" * 5


def test_the_token_is_required_with_the_bundled_idp(make_settings) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ConfigError) as caught:
        make_settings(ATLAS_IDP_PUBLIC_URL=IDP, ATLAS_IDP_ADMIN_TOKEN=None)
    assert caught.value.problems == [
        "ATLAS_IDP_ADMIN_TOKEN: is required with the bundled IdP "
        "(`atlas-admin idp-bootstrap` writes it into atlas-idp/idp.env)"
    ]


@pytest.mark.parametrize("empty", ["", "   "])
def test_an_empty_token_is_missing_too(make_settings, empty: str) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ConfigError, match="ATLAS_IDP_ADMIN_TOKEN: is required"):
        make_settings(ATLAS_IDP_PUBLIC_URL=IDP, ATLAS_IDP_ADMIN_TOKEN=empty.strip() or "")


def test_the_error_names_the_variable_and_never_a_value() -> None:
    with pytest.raises(ConfigError) as caught:
        build_settings(
            _env_file=None, **{**VALID, "ATLAS_IDP_PUBLIC_URL": IDP, "ATLAS_IDP_ADMIN_TOKEN": ""}
        )
    assert "ATLAS_IDP_ADMIN_TOKEN" in str(caught.value)
    assert VALID["SECRET_KEY"] not in str(caught.value)


def test_byo_needs_no_token(make_settings) -> None:  # type: ignore[no-untyped-def]
    settings = make_settings()
    assert settings.bundled_idp is False and settings.idp_admin_token is None


def test_the_token_is_a_secret(make_settings) -> None:  # type: ignore[no-untyped-def]
    settings = make_settings(ATLAS_IDP_PUBLIC_URL=IDP, ATLAS_IDP_ADMIN_TOKEN=TOKEN_VALUE)
    assert settings.idp_admin_token is not None
    assert settings.idp_admin_token.get_secret_value() == TOKEN_VALUE
    assert TOKEN_VALUE not in repr(settings) and TOKEN_VALUE not in str(settings)


def test_defaults_match_docker_compose(make_settings) -> None:  # type: ignore[no-untyped-def]
    settings = make_settings(ATLAS_IDP_PUBLIC_URL=IDP)
    assert settings.idp_internal_url == "https://idp:8443"
    assert settings.idp_ca_file == Path("/run/secrets/idp-trust/ca.pem")
    assert settings.idp_groups_file == Path("/run/secrets/atlas-idp/groups.json")


def test_the_defaults_are_where_the_compose_file_mounts_them() -> None:
    compose = Path("deploy/docker-compose.yml").read_text()
    api = compose.split("  atlas-api:")[1].split("  # --- Bundled IdP")[0]
    assert "/idp-trust:/run/secrets/idp-trust:ro" in api  # the CA
    assert "/atlas-idp:/run/secrets/atlas-idp:ro" in api  # groups.json and idp.env


@pytest.mark.parametrize(
    ("given", "stored"),
    [
        ("https://idp:8443", "https://idp:8443"),
        ("https://idp:8443/", "https://idp:8443"),
        ("https://kanidm.internal.test.invalid", "https://kanidm.internal.test.invalid"),
    ],
)
def test_the_internal_url_is_an_https_origin(make_settings, given: str, stored: str) -> None:  # type: ignore[no-untyped-def]
    settings = make_settings(ATLAS_IDP_PUBLIC_URL=IDP, ATLAS_IDP_INTERNAL_URL=given)
    assert settings.idp_internal_url == stored


@pytest.mark.parametrize(
    "bad",
    [
        "http://idp:8443",
        "idp:8443",
        "https://idp:8443/path",
        "https://u:p@idp:8443",
        "https://idp?x=1",
    ],
)
def test_a_bad_internal_url_is_refused(make_settings, bad: str) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ConfigError, match="ATLAS_IDP_INTERNAL_URL"):
        make_settings(ATLAS_IDP_PUBLIC_URL=IDP, ATLAS_IDP_INTERNAL_URL=bad)


def test_an_empty_internal_url_means_the_default(make_settings) -> None:  # type: ignore[no-untyped-def]
    settings = make_settings(ATLAS_IDP_PUBLIC_URL=IDP, ATLAS_IDP_INTERNAL_URL="")
    assert settings.idp_internal_url == "https://idp:8443"


def test_the_paths_can_be_set(make_settings) -> None:  # type: ignore[no-untyped-def]
    settings = make_settings(
        ATLAS_IDP_PUBLIC_URL=IDP,
        ATLAS_IDP_CA_FILE="/somewhere/ca.pem",
        ATLAS_IDP_GROUPS_FILE="/somewhere/groups.json",
    )
    assert settings.idp_ca_file == Path("/somewhere/ca.pem")
    assert settings.idp_groups_file == Path("/somewhere/groups.json")
