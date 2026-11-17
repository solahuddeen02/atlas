"""Settings added for the IdP bootstrap (PLAN Phase 5b-2b): ATLAS_IMMICH_PUBLIC_URL and
ATLAS_IDP_ADMIN_TOKEN, and the env-file order that lets the bootstrap's secret win."""

from pathlib import Path

import pytest

from atlas.kernel.config import (
    BootstrapSettings,
    ConfigError,
    check_immich_public_url,
    load_bootstrap_settings,
    load_settings,
)
from atlas.kernel.identity.record import BOOTSTRAP_VERSION, record_text
from tests.conftest import VALID

BASE = {
    "ATLAS_PUBLIC_URL": "https://atlas.home.arpa",
    "ATLAS_IDP_PUBLIC_URL": "https://id.home.arpa",
}


def load(monkeypatch: pytest.MonkeyPatch, **env: str) -> BootstrapSettings:
    for name, value in {**BASE, **env}.items():
        monkeypatch.setenv(name, value)
    return load_bootstrap_settings()


def problems(monkeypatch: pytest.MonkeyPatch, **env: str) -> list[str]:
    with pytest.raises(ConfigError) as caught:
        load(monkeypatch, **env)
    return caught.value.problems


def test_the_minimum_is_the_two_public_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = load(monkeypatch)
    assert settings.public_url == "https://atlas.home.arpa"
    assert settings.idp_public_url == "https://id.home.arpa"
    assert settings.immich_public_url is None


@pytest.mark.parametrize("empty", ["", "  "])
def test_an_empty_immich_url_means_no_immich(monkeypatch: pytest.MonkeyPatch, empty: str) -> None:
    # docker-compose.yml passes ${ATLAS_IMMICH_PUBLIC_URL:-}, i.e. "" when it is not set.
    assert load(monkeypatch, ATLAS_IMMICH_PUBLIC_URL=empty).immich_public_url is None


@pytest.mark.parametrize(
    ("given", "stored"),
    [
        ("https://immich.home.arpa", "https://immich.home.arpa"),
        ("https://immich.home.arpa/", "https://immich.home.arpa"),
        ("https://Immich.Home.Arpa", "https://immich.home.arpa"),
        ("https://immich.home.arpa:443", "https://immich.home.arpa"),
        ("https://immich.home.arpa:2283", "https://immich.home.arpa:2283"),
    ],
)
def test_the_immich_url_is_an_https_origin(
    monkeypatch: pytest.MonkeyPatch, given: str, stored: str
) -> None:
    assert load(monkeypatch, ATLAS_IMMICH_PUBLIC_URL=given).immich_public_url == stored


@pytest.mark.parametrize(
    ("given", "message"),
    [
        ("http://immich.home.arpa", "must be an absolute https:// URL"),
        ("immich.home.arpa", "must be an absolute https:// URL"),
        ("https://", "must be an absolute https:// URL"),
        ("https://immich.home.arpa/photos", "must be an origin with no path"),
        ("https://immich.home.arpa/?a=b", "must be an origin with no path"),
        ("https://immich.home.arpa/#x", "must be an origin with no path"),
        ("https://user:hunter2@immich.home.arpa", "must not contain a user name or password"),
        ("https://immich.home.arpa:notaport", "invalid port"),
        ("https://bad host.home.arpa", "invalid host name"),
        ("https://immich.example.com", "still uses a placeholder domain"),
    ],
)
def test_the_immich_url_rules(monkeypatch: pytest.MonkeyPatch, given: str, message: str) -> None:
    found = problems(monkeypatch, ATLAS_IMMICH_PUBLIC_URL=given)
    assert len(found) == 1
    assert found[0].startswith("ATLAS_IMMICH_PUBLIC_URL: ") and message in found[0]
    # An error names the setting and the rule, never the value.
    assert "hunter2" not in found[0] and "immich.home.arpa" not in found[0]


def test_a_placeholder_immich_domain_is_fine_outside_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = load(
        monkeypatch,
        ATLAS_ENV="development",
        ATLAS_PUBLIC_URL="https://atlas.example.com",
        ATLAS_IDP_PUBLIC_URL="https://id.example.com",
        ATLAS_IMMICH_PUBLIC_URL="https://immich.example.com",
    )
    assert settings.immich_public_url == "https://immich.example.com"


def test_check_immich_public_url_is_usable_on_its_own() -> None:
    assert check_immich_public_url("https://immich.home.arpa/", production=True) == (
        "https://immich.home.arpa"
    )


@pytest.mark.parametrize("host", ["atlas.home.arpa", "id.home.arpa"])
def test_immich_has_its_own_host(monkeypatch: pytest.MonkeyPatch, host: str) -> None:
    found = problems(monkeypatch, ATLAS_IMMICH_PUBLIC_URL=f"https://{host}")
    assert found == [
        "ATLAS_IMMICH_PUBLIC_URL: must use a different host name than ATLAS_PUBLIC_URL and "
        "ATLAS_IDP_PUBLIC_URL (Immich has its own origin)"
    ]


def test_the_idp_has_its_own_host(monkeypatch: pytest.MonkeyPatch) -> None:
    found = problems(monkeypatch, ATLAS_IDP_PUBLIC_URL="https://atlas.home.arpa")
    assert found == [
        "ATLAS_IDP_PUBLIC_URL: must use a different host name than ATLAS_PUBLIC_URL "
        "(the IdP needs its own origin)"
    ]


@pytest.mark.parametrize("missing", ["ATLAS_PUBLIC_URL", "ATLAS_IDP_PUBLIC_URL"])
def test_both_public_urls_are_required(monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    for name, value in BASE.items():
        if name != missing:
            monkeypatch.setenv(name, value)
    with pytest.raises(ConfigError) as caught:
        load_bootstrap_settings()
    assert caught.value.problems == [f"{missing}: is required"]


def test_the_bootstrap_loads_no_env_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Nothing secret is read: a file named in ATLAS_ENV_FILES is ignored.
    monkeypatch.setenv("ATLAS_ENV_FILES", str(tmp_path / "does-not-exist.env"))
    assert load(monkeypatch).public_url == "https://atlas.home.arpa"


# --- ATLAS_IDP_ADMIN_TOKEN, and the order of the env files ---------------------------

ADMIN_VALUE = "console-" * 5


def test_the_admin_token_is_optional_and_never_shown(make_settings) -> None:  # type: ignore[no-untyped-def]
    assert make_settings().idp_admin_token is None
    settings = make_settings(ATLAS_IDP_ADMIN_TOKEN=ADMIN_VALUE)
    assert settings.idp_admin_token is not None
    assert settings.idp_admin_token.get_secret_value() == ADMIN_VALUE
    assert ADMIN_VALUE not in repr(settings) and ADMIN_VALUE not in str(settings)


def test_idp_env_listed_last_wins_over_a_secret_set_by_hand(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app_env = tmp_path / "app.env"
    app_env.write_text(
        "".join(
            f"{k}={v}\n"
            for k, v in {
                "SECRET_KEY": VALID["SECRET_KEY"],
                "DATABASE_URL": VALID["DATABASE_URL"],
                # Set by hand before the bootstrap existed:
                "ATLAS_OIDC_CLIENT_SECRET": "set-by-hand-1",
            }.items()
        )
    )
    connectors = tmp_path / "connectors.env"
    connectors.write_text("")
    idp_env = tmp_path / "idp.env"
    idp_env.write_text(
        f"ATLAS_OIDC_CLIENT_SECRET=from-the-idp-2\nATLAS_IDP_ADMIN_TOKEN={ADMIN_VALUE}\n"
    )
    monkeypatch.setenv("ATLAS_ENV", "development")
    monkeypatch.setenv("ATLAS_PUBLIC_URL", "https://atlas.home.arpa")
    monkeypatch.setenv("ATLAS_IDP_PUBLIC_URL", "https://id.home.arpa")
    record = tmp_path / "bootstrap.json"
    record.write_text(record_text(digest="0" * 64, complete=True))
    monkeypatch.setenv("ATLAS_IDP_BOOTSTRAP_RECORD", str(record))

    # The order docker-compose.yml gives atlas-api with the bundled IdP.
    monkeypatch.setenv("ATLAS_ENV_FILES", f"{app_env},{connectors},{idp_env}")
    bundled = load_settings()
    assert bundled.oidc_client_secret.get_secret_value() == "from-the-idp-2"
    assert bundled.idp_admin_token is not None
    assert bundled.idp_admin_token.get_secret_value() == ADMIN_VALUE
    assert bundled.oidc_issuer == "https://id.home.arpa/oauth2/openid/atlas"

    # BYO: idp.env is not listed, and the value in app.env stays.
    monkeypatch.setenv("ATLAS_ENV_FILES", f"{app_env},{connectors}")
    monkeypatch.setenv("ATLAS_OIDC_ISSUER", "https://sso.home.arpa")
    monkeypatch.delenv("ATLAS_IDP_PUBLIC_URL")
    byo = load_settings()
    assert byo.oidc_client_secret.get_secret_value() == "set-by-hand-1"
    assert byo.idp_admin_token is None


# --- atlas-api refuses to start unless the IdP bootstrap completed ------------------------


def bundled_api(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, idp: bool = True) -> Path:
    """Env files and variables for load_settings(); returns the bootstrap record's path."""
    app_env = tmp_path / "app.env"
    app_env.write_text(
        f"SECRET_KEY={VALID['SECRET_KEY']}\nDATABASE_URL={VALID['DATABASE_URL']}\n"
        "ATLAS_OIDC_CLIENT_SECRET=set-by-hand-1\n"
    )
    monkeypatch.setenv("ATLAS_ENV", "development")
    monkeypatch.setenv("ATLAS_PUBLIC_URL", "https://atlas.home.arpa")
    monkeypatch.setenv("ATLAS_ENV_FILES", str(app_env))
    record = tmp_path / "bootstrap.json"
    monkeypatch.setenv("ATLAS_IDP_BOOTSTRAP_RECORD", str(record))
    if idp:
        monkeypatch.setenv("ATLAS_IDP_PUBLIC_URL", "https://id.home.arpa")
    else:
        monkeypatch.setenv("ATLAS_OIDC_ISSUER", "https://sso.home.arpa")
    return record


def test_a_complete_record_lets_the_bundled_api_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    record = bundled_api(monkeypatch, tmp_path)
    record.write_text(record_text(digest="0" * 64, complete=True))
    assert load_settings().bundled_idp is True


def test_a_missing_record_stops_the_bundled_api(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bundled_api(monkeypatch, tmp_path)
    with pytest.raises(ConfigError) as caught:
        load_settings()
    (problem,) = caught.value.problems
    assert problem.startswith("ATLAS_IDP_BOOTSTRAP_RECORD: ")
    assert "has not run yet" in problem
    assert "docker compose logs atlas-idp-bootstrap" in problem


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        ({"version": BOOTSTRAP_VERSION, "digest": "x", "complete": False}, "has not completed"),
        ({"version": BOOTSTRAP_VERSION, "digest": "x"}, "has not completed"),
        (
            {"version": BOOTSTRAP_VERSION - 1, "digest": "x", "complete": True},
            f"from version {BOOTSTRAP_VERSION - 1}",
        ),
        (
            {"version": BOOTSTRAP_VERSION + 1, "digest": "x", "complete": True},
            f"from version {BOOTSTRAP_VERSION + 1}",
        ),
        ("not json", "unreadable"),
    ],
)
def test_an_incomplete_or_old_record_stops_the_bundled_api_with_the_old_files_still_there(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, content: object, reason: str
) -> None:
    import json

    record = bundled_api(monkeypatch, tmp_path)
    record.write_text(content if isinstance(content, str) else json.dumps(content))
    with pytest.raises(ConfigError) as caught:
        load_settings()
    (problem,) = caught.value.problems
    assert reason in problem and "docker compose logs atlas-idp-bootstrap" in problem


def test_byo_is_not_checked(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    record = bundled_api(monkeypatch, tmp_path, idp=False)
    assert not record.exists()
    assert load_settings().bundled_idp is False


def test_directly_built_settings_are_not_checked() -> None:
    from atlas.kernel.config import build_settings

    settings = build_settings(
        _env_file=None, **{**VALID, "ATLAS_IDP_PUBLIC_URL": "https://id.home.arpa"}
    )
    assert settings.bundled_idp is True  # no record file anywhere, and that is fine here


def test_a_missing_idp_env_points_at_the_bootstrap_not_at_init(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app_env = tmp_path / "app.env"
    app_env.write_text("")
    idp_env = tmp_path / "idp.env"
    monkeypatch.setenv("ATLAS_ENV_FILES", f"{app_env},{idp_env}")
    with pytest.raises(ConfigError) as caught:
        load_settings()
    (problem,) = caught.value.problems
    assert "written by the IdP bootstrap" in problem
    assert "docker compose logs atlas-idp-bootstrap" in problem
    assert "atlas-admin init" not in problem
    # Any other missing file still points at init.
    monkeypatch.setenv("ATLAS_ENV_FILES", str(tmp_path / "app2.env"))
    with pytest.raises(ConfigError) as other:
        load_settings()
    assert "run `atlas-admin init`" in other.value.problems[0]


def test_the_record_path_is_a_setting_with_the_mounted_default(make_settings) -> None:  # type: ignore[no-untyped-def]
    assert str(make_settings().idp_bootstrap_record) == "/run/secrets/atlas-idp/bootstrap.json"
    custom = make_settings(ATLAS_IDP_BOOTSTRAP_RECORD="/elsewhere/bootstrap.json")
    assert str(custom.idp_bootstrap_record) == "/elsewhere/bootstrap.json"
