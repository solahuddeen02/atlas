"""Settings of sessions and the OIDC relying party (Phase 5c-1, SELF_HOSTING)."""

from collections.abc import Callable
from datetime import timedelta
from ipaddress import ip_network

import pytest

from atlas.kernel.config import ConfigError, Settings, parse_duration
from atlas.kernel.identity.ratelimit import RateSpec

Make = Callable[..., Settings]
IDP = "https://id.home.arpa"


def test_the_defaults_are_what_the_bundled_idp_allows(make_settings: Make) -> None:
    settings = make_settings()
    for role, idle, absolute in (
        ("owner", 4, 8),
        ("admin", 4, 8),
        ("adult", 12, 24),
        ("child", 12, 24),
        ("guest", 12, 24),
    ):
        lifetimes = settings.session_lifetimes(role)
        assert lifetimes.idle == timedelta(hours=idle), role
        assert lifetimes.absolute == timedelta(hours=absolute), role
    assert settings.step_up_max_age == 300
    assert settings.oidc_prompt_login is True
    assert settings.session_revalidate_interval == 300
    assert settings.session_revalidate_grace == 900
    assert (settings.rate_auth_ip, settings.rate_auth_account) == (
        RateSpec(20, 60),
        RateSpec(10, 60),
    )


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("5m", 300),
        ("30m", 1800),
        ("12h", 43200),
        ("1d", 86400),
        ("7d", 604800),
        ("90d", 7776000),
        (" 2H ", 7200),
    ],
)
def test_a_duration_is_written_with_a_unit(make_settings: Make, text: str, seconds: int) -> None:
    assert parse_duration(text) == seconds
    settings = make_settings(ATLAS_SESSION_IDLE_ADULT="5m", ATLAS_SESSION_MAX_ADULT=text)
    assert settings.session_max_adult == seconds


@pytest.mark.parametrize(
    "bad",
    ["", "12", "12s", "h", "0h", "-1h", "1.5h", "4 h", "1w", "4m", "91d", "9999d", "true", "5h30m"],
)
def test_a_bad_duration_is_refused_without_its_value(make_settings: Make, bad: str) -> None:
    with pytest.raises(ConfigError) as caught:
        make_settings(ATLAS_SESSION_MAX_ADULT=bad)
    (problem,) = caught.value.problems
    assert problem in {
        "ATLAS_SESSION_MAX_ADULT: must be a duration with a unit: 30m, 12h or 7d",
        "ATLAS_SESSION_MAX_ADULT: must be between 5 minutes and 90 days",
    }


def test_every_role_has_its_own_variables(make_settings: Make) -> None:
    settings = make_settings(
        ATLAS_SESSION_IDLE_OWNER="1h",
        ATLAS_SESSION_MAX_OWNER="2h",
        ATLAS_SESSION_IDLE_GUEST="30m",
        ATLAS_SESSION_MAX_GUEST="1d",
    )
    assert settings.session_lifetimes("owner").idle == timedelta(hours=1)
    assert settings.session_lifetimes("owner").absolute == timedelta(hours=2)
    assert settings.session_lifetimes("guest").idle == timedelta(minutes=30)
    assert settings.session_lifetimes("adult").idle == timedelta(hours=12)  # untouched


def test_the_idle_time_must_not_exceed_the_absolute_time(make_settings: Make) -> None:
    with pytest.raises(ConfigError) as caught:
        make_settings(ATLAS_SESSION_IDLE_CHILD="2d", ATLAS_SESSION_MAX_CHILD="1d")
    assert caught.value.problems == [
        "ATLAS_SESSION_IDLE_CHILD: must not exceed ATLAS_SESSION_MAX_CHILD"
    ]


def test_an_unknown_role_is_refused(make_settings: Make) -> None:
    with pytest.raises(ValueError, match="unknown role"):
        make_settings().session_lifetimes("root")


@pytest.mark.parametrize("value", ["30", "3600", "300"])
def test_step_up_max_age_in_range(make_settings: Make, value: str) -> None:
    assert make_settings(ATLAS_STEP_UP_MAX_AGE=value).step_up_max_age == int(value)


@pytest.mark.parametrize("value", ["29", "3601", "0", "-5", "abc", "5m"])
def test_step_up_max_age_out_of_range(make_settings: Make, value: str) -> None:
    with pytest.raises(ConfigError, match="ATLAS_STEP_UP_MAX_AGE"):
        make_settings(ATLAS_STEP_UP_MAX_AGE=value)


@pytest.mark.parametrize(
    ("value", "expected"), [("false", False), ("true", True), ("0", False), ("1", True)]
)
def test_prompt_login_is_a_switch(make_settings: Make, value: str, expected: bool) -> None:
    assert make_settings(ATLAS_OIDC_PROMPT_LOGIN=value).oidc_prompt_login is expected


def test_prompt_login_refuses_a_maybe(make_settings: Make) -> None:
    with pytest.raises(ConfigError, match="ATLAS_OIDC_PROMPT_LOGIN"):
        make_settings(ATLAS_OIDC_PROMPT_LOGIN="maybe")


def test_the_default_scopes_depend_on_the_mode(make_settings: Make) -> None:
    assert make_settings().oidc_scopes == "openid profile"  # BYO: the provider may lack `groups`
    bundled = make_settings(ATLAS_IDP_PUBLIC_URL=IDP, ATLAS_OIDC_ISSUER=None)
    assert bundled.oidc_scopes == "openid profile groups"


def test_scopes_can_be_set_and_must_contain_openid(make_settings: Make) -> None:
    assert (
        make_settings(ATLAS_OIDC_SCOPES="openid  email   profile").oidc_scopes
        == "openid email profile"
    )
    assert make_settings(ATLAS_OIDC_SCOPES="openid openid profile").oidc_scopes == "openid profile"
    assert (
        make_settings(ATLAS_OIDC_SCOPES="  ").oidc_scopes == "openid profile"
    )  # empty: the default
    for bad in ("profile email", "openidx", 'openid "x"', "openid a\\b"):
        with pytest.raises(ConfigError, match="ATLAS_OIDC_SCOPES"):
            make_settings(ATLAS_OIDC_SCOPES=bad)


@pytest.mark.parametrize(
    ("value", "ok"), [("5", True), ("3600", True), ("4", False), ("3601", False), ("x", False)]
)
def test_revalidate_interval_bounds(make_settings: Make, value: str, ok: bool) -> None:
    if ok:
        assert make_settings(
            ATLAS_SESSION_REVALIDATE_INTERVAL=value, ATLAS_SESSION_REVALIDATE_GRACE="3600"
        ).session_revalidate_interval == int(value)
    else:
        with pytest.raises(ConfigError, match="ATLAS_SESSION_REVALIDATE_INTERVAL"):
            make_settings(ATLAS_SESSION_REVALIDATE_INTERVAL=value)


def test_the_grace_must_cover_one_interval(make_settings: Make) -> None:
    with pytest.raises(ConfigError) as caught:
        make_settings(ATLAS_SESSION_REVALIDATE_INTERVAL="600", ATLAS_SESSION_REVALIDATE_GRACE="300")
    assert caught.value.problems == [
        "ATLAS_SESSION_REVALIDATE_GRACE: must be at least ATLAS_SESSION_REVALIDATE_INTERVAL"
    ]
    ok = make_settings(
        ATLAS_SESSION_REVALIDATE_INTERVAL="600", ATLAS_SESSION_REVALIDATE_GRACE="600"
    )
    assert ok.session_revalidate_grace == 600


@pytest.mark.parametrize("bad", ["0r/m", "20", "20r/h", "r/m", "20 r/m", "-1r/s"])
def test_a_bad_app_rate_is_refused(make_settings: Make, bad: str) -> None:
    with pytest.raises(ConfigError, match="ATLAS_APP_RATE_AUTH_IP: must look like 20r/m or 5r/s"):
        make_settings(ATLAS_APP_RATE_AUTH_IP=bad)
    with pytest.raises(ConfigError, match="ATLAS_APP_RATE_AUTH_ACCOUNT: must look like"):
        make_settings(ATLAS_APP_RATE_AUTH_ACCOUNT=bad)


def test_the_app_rates_are_read(make_settings: Make) -> None:
    settings = make_settings(ATLAS_APP_RATE_AUTH_IP="5r/s", ATLAS_APP_RATE_AUTH_ACCOUNT="3r/m")
    assert settings.rate_auth_ip == RateSpec(5, 1)
    assert settings.rate_auth_account == RateSpec(3, 60)


def test_the_trusted_peers_default_to_loopback_and_private_ranges(make_settings: Make) -> None:
    peers = make_settings().api_trusted_peers
    assert {str(p) for p in peers} == {
        "127.0.0.0/8",
        "::1/128",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "fc00::/7",
    }
    assert not any(ip_network("8.8.8.8/32").subnet_of(p) for p in peers if p.version == 4)


def test_the_trusted_peers_can_be_listed_or_turned_off(make_settings: Make) -> None:
    listed = make_settings(ATLAS_API_TRUSTED_PEERS="172.30.0.0/16, 2001:db8::/32")
    assert [str(p) for p in listed.api_trusted_peers] == ["172.30.0.0/16", "2001:db8::/32"]
    assert make_settings(ATLAS_API_TRUSTED_PEERS="none").api_trusted_peers == ()
    assert make_settings(ATLAS_API_TRUSTED_PEERS="NONE").api_trusted_peers == ()


@pytest.mark.parametrize("bad", ["not-a-cidr", "10.0.0.0/33", "10.0.0.0/8,oops", "everyone"])
def test_bad_trusted_peers_are_refused(make_settings: Make, bad: str) -> None:
    with pytest.raises(ConfigError) as caught:
        make_settings(ATLAS_API_TRUSTED_PEERS=bad)
    (problem,) = caught.value.problems
    assert problem == (
        "ATLAS_API_TRUSTED_PEERS: must be a comma-separated list of CIDR ranges, or `none`"
    )


def test_no_session_setting_leaks_a_secret_in_an_error(make_settings: Make) -> None:
    with pytest.raises(ConfigError) as caught:
        make_settings(ATLAS_SESSION_MAX_OWNER="nope", ATLAS_STEP_UP_MAX_AGE="9")
    text = str(caught.value)
    from tests.conftest import VALID

    assert VALID["SECRET_KEY"] not in text and VALID["ATLAS_OIDC_CLIENT_SECRET"] not in text
