"""`current_session`, CSRF, Origin, step-up and cookies (ADR-003 §4, Phase 5c-1)."""

import logging
from collections.abc import Callable
from datetime import timedelta
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, text

from atlas.kernel.config import Settings
from atlas.kernel.db.engine import make_engine
from atlas.kernel.identity.cookies import (
    clear_cookie,
    login_cookie_name,
    session_cookie_name,
    set_cookie,
)
from atlas.kernel.identity.ratelimit import RateLimiter, RateSpec
from atlas.kernel.identity.sessions import IssuedSession, Session, SessionStore
from atlas.kernel.identity.tokenbox import TokenBox
from atlas.kernel.web import create_app
from atlas.kernel.web.auth import (
    ApiError,
    client_address,
    current_session,
    get_rate_limiter,
    require_step_up,
)
from tests.kernel.conftest import FrozenClock, MakeUser

PUBLIC = "https://atlas.home.arpa"
NOT_AUTHENTICATED = {"error": {"code": "not_authenticated", "message": "Sign in to continue."}}


def build_app(settings: Settings, engine: Engine, clock: FrozenClock) -> FastAPI:
    app = create_app(settings, engine=engine, clock=clock)

    @app.get("/t/me")
    def me(session: Annotated[Session, Depends(current_session)]) -> dict[str, str]:
        return {"user": session.username, "role": session.role.value}

    @app.api_route("/t/change", methods=["POST", "PUT", "PATCH", "DELETE"])
    def change(session: Annotated[Session, Depends(current_session)]) -> dict[str, bool]:
        return {"ok": True}

    @app.get("/t/admin")
    def admin(session: Annotated[Session, Depends(require_step_up())]) -> dict[str, bool]:
        return {"ok": True}

    @app.get("/t/strict")
    def strict(
        session: Annotated[Session, Depends(require_step_up(max_age=30))],
    ) -> dict[str, bool]:
        return {"ok": True}

    @app.get("/t/cookies")
    def cookies(response: Response) -> dict[str, bool]:
        secure = settings.cookie_secure
        set_cookie(
            response, session_cookie_name(secure=secure), "x" * 43, secure=secure, max_age=60
        )
        set_cookie(response, login_cookie_name(secure=secure), "y" * 43, secure=secure, max_age=60)
        return {"ok": True}

    @app.get("/t/clear")
    def clear(response: Response) -> dict[str, bool]:
        clear_cookie(
            response,
            session_cookie_name(secure=settings.cookie_secure),
            secure=settings.cookie_secure,
        )
        return {"ok": True}

    return app


@pytest.fixture
def production(make_settings: Callable[..., Settings]) -> Settings:
    return make_settings(ATLAS_ENV="production")


@pytest.fixture
def client(settings: Settings, app_engine: Engine, clock: FrozenClock) -> TestClient:
    return TestClient(build_app(settings, app_engine, clock), base_url=PUBLIC)


def sign_in(
    store: SessionStore, make_user: MakeUser, clock: FrozenClock, **kwargs: str
) -> tuple[IssuedSession, dict[str, str]]:
    issued = store.create(make_user(**kwargs), auth_time=clock.now)
    return issued, {"Cookie": f"atlas_session={issued.token.reveal()}"}


# --- authentication ---------------------------------------------------------------------


def test_no_cookie_is_a_401_with_the_api_error_body(client: TestClient) -> None:
    response = client.get("/t/me")
    assert response.status_code == 401
    assert response.json() == NOT_AUTHENTICATED


def test_a_valid_session_names_the_person(
    client: TestClient, store: SessionStore, make_user: MakeUser, clock: FrozenClock
) -> None:
    _, cookie = sign_in(store, make_user, clock, role="admin", username="malee")
    response = client.get("/t/me", headers=cookie)
    assert response.status_code == 200
    assert response.json() == {"user": "malee", "role": "admin"}


def test_a_revoked_session_is_refused_on_the_very_next_request(
    client: TestClient, store: SessionStore, make_user: MakeUser, clock: FrozenClock
) -> None:
    issued, cookie = sign_in(store, make_user, clock)
    assert client.get("/t/me", headers=cookie).status_code == 200
    store.revoke(issued.session.id, "admin")
    assert client.get("/t/me", headers=cookie).status_code == 401


def test_a_disabled_person_is_refused(
    client: TestClient,
    store: SessionStore,
    app_engine: Engine,
    make_user: MakeUser,
    clock: FrozenClock,
) -> None:
    issued, cookie = sign_in(store, make_user, clock)
    with app_engine.begin() as connection:
        connection.execute(
            text("UPDATE kernel.users SET status = 'disabled' WHERE id = :u"),
            {"u": issued.session.user_id},
        )
    assert client.get("/t/me", headers=cookie).status_code == 401


def test_every_reason_for_refusing_looks_the_same(
    client: TestClient,
    store: SessionStore,
    app_engine: Engine,
    make_user: MakeUser,
    clock: FrozenClock,
) -> None:
    """Nothing tells the caller whether a cookie is unknown, revoked, idle or a person off."""
    revoked, revoked_cookie = sign_in(store, make_user, clock)
    store.revoke(revoked.session.id, "user")
    idle, idle_cookie = sign_in(store, make_user, clock, role="owner")
    with app_engine.begin() as connection:
        connection.execute(
            text("UPDATE kernel.sessions SET idle_expires_at = :t WHERE id = :i"),
            {"t": clock.now - timedelta(seconds=1), "i": idle.session.id},
        )
    off, off_cookie = sign_in(store, make_user, clock)
    with app_engine.begin() as connection:
        connection.execute(
            text("UPDATE kernel.users SET status = 'disabled' WHERE id = :u"),
            {"u": off.session.user_id},
        )
    responses = [
        client.get("/t/me", headers=headers)
        for headers in (
            revoked_cookie,
            idle_cookie,
            off_cookie,
            {"Cookie": "atlas_session=" + "A" * 43},
            {"Cookie": "atlas_session=short"},
            {},
        )
    ]
    assert {(r.status_code, r.text) for r in responses} == {(401, client.get("/t/me").text)}
    assert responses[0].json() == NOT_AUTHENTICATED


def test_a_database_that_cannot_be_asked_is_never_a_yes(
    settings: Settings, clock: FrozenClock
) -> None:
    engine = make_engine(SecretStr("postgresql+psycopg://nobody:nothing@127.0.0.1:1/none"))
    try:
        client = TestClient(build_app(settings, engine, clock), base_url=PUBLIC)
        response = client.get("/t/me", headers={"Cookie": "atlas_session=" + "A" * 43})
    finally:
        engine.dispose()
    assert response.status_code == 503
    assert response.json() == {
        "error": {"code": "unavailable", "message": "Try again in a moment."}
    }


# --- CSRF and Origin on unsafe methods ------------------------------------------------------------


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_an_unsafe_request_needs_the_csrf_token(
    client: TestClient,
    store: SessionStore,
    make_user: MakeUser,
    clock: FrozenClock,
    method: str,
) -> None:
    issued, cookie = sign_in(store, make_user, clock)
    csrf = issued.session.csrf_token
    assert csrf
    refused = {"error": {"code": "csrf_failed", "message": "The request was refused."}}
    for headers in (
        cookie,  # no token
        {**cookie, "X-CSRF-Token": ""},
        {**cookie, "X-CSRF-Token": "wrong"},
        {**cookie, "X-CSRF-Token": csrf[:-1]},
        {**cookie, "X-CSRF-Token": csrf + "x"},
        {**cookie, "X-CSRF-Token": issued.token.reveal()},  # the cookie value is not the token
    ):
        response = client.request(method, "/t/change", headers=headers)
        assert (response.status_code, response.json()) == (403, refused)
    ok = client.request(method, "/t/change", headers={**cookie, "X-CSRF-Token": csrf})
    assert ok.status_code == 200


def test_a_safe_request_needs_no_token_and_ignores_the_origin(
    client: TestClient, store: SessionStore, make_user: MakeUser, clock: FrozenClock
) -> None:
    _, cookie = sign_in(store, make_user, clock)
    assert client.get("/t/me", headers=cookie).status_code == 200
    foreign = {**cookie, "Origin": "https://evil.example.net"}
    assert client.get("/t/me", headers=foreign).status_code == 200


def test_an_unsafe_request_without_a_session_is_a_401_not_a_403(client: TestClient) -> None:
    response = client.post("/t/change", headers={"X-CSRF-Token": "anything"})
    assert (response.status_code, response.json()) == (401, NOT_AUTHENTICATED)


def test_a_session_without_a_stored_csrf_token_fails_every_unsafe_request(
    client: TestClient,
    store: SessionStore,
    app_engine: Engine,
    make_user: MakeUser,
    clock: FrozenClock,
) -> None:
    issued, cookie = sign_in(store, make_user, clock)
    with app_engine.begin() as connection:
        connection.execute(
            text("UPDATE kernel.sessions SET csrf_token = NULL WHERE id = :i"),
            {"i": issued.session.id},
        )
    for token in ("", "None", "null", "undefined", "0"):
        headers = {**cookie, "X-CSRF-Token": token}
        assert client.post("/t/change", headers=headers).status_code == 403
    assert client.post("/t/change", headers=cookie).status_code == 403
    assert client.get("/t/me", headers=cookie).status_code == 200  # reading still works


@pytest.mark.parametrize(
    "origin",
    [
        "https://evil.example.net",  # another site
        "http://atlas.home.arpa",  # the same host over HTTP
        "https://atlas.home.arpa:8443",  # another port
        "https://atlas.home.arpa.evil.example.net",  # a look-alike
        "https://evil.example.net/https://atlas.home.arpa",
        "https://atlas.home.arpa@evil.example.net",
        "null",  # what a sandboxed frame or a redirect sends
        "",
        "atlas.home.arpa",
        "https://",
    ],
)
def test_a_foreign_origin_is_refused_even_with_a_valid_csrf_token(
    client: TestClient,
    store: SessionStore,
    make_user: MakeUser,
    clock: FrozenClock,
    origin: str,
) -> None:
    issued, cookie = sign_in(store, make_user, clock)
    headers = {**cookie, "X-CSRF-Token": issued.session.csrf_token or "", "Origin": origin}
    response = client.post("/t/change", headers=headers)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "origin_mismatch"


@pytest.mark.parametrize(
    "origin", [PUBLIC, "https://ATLAS.HOME.ARPA", "https://atlas.home.arpa:443", PUBLIC + "/"]
)
def test_the_public_origin_passes_with_a_valid_csrf_token(
    client: TestClient,
    store: SessionStore,
    make_user: MakeUser,
    clock: FrozenClock,
    origin: str,
) -> None:
    issued, cookie = sign_in(store, make_user, clock)
    headers = {**cookie, "X-CSRF-Token": issued.session.csrf_token or "", "Origin": origin}
    assert client.post("/t/change", headers=headers).status_code == 200


def test_two_origin_headers_are_refused(
    client: TestClient, store: SessionStore, make_user: MakeUser, clock: FrozenClock
) -> None:
    issued, cookie = sign_in(store, make_user, clock)
    response = client.post(
        "/t/change",
        headers=[
            ("Cookie", cookie["Cookie"]),
            ("X-CSRF-Token", issued.session.csrf_token or ""),
            ("Origin", PUBLIC),
            ("Origin", PUBLIC),
        ],
    )
    assert response.status_code == 403


def test_the_origin_is_judged_before_the_session_and_before_the_token(client: TestClient) -> None:
    # No session and no token, and a foreign Origin: the Origin answers, without a lookup.
    response = client.post("/t/change", headers={"Origin": "https://evil.example.net"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "origin_mismatch"


def test_the_origin_follows_the_configured_public_url(
    make_settings: Callable[..., Settings],
    app_engine: Engine,
    clock: FrozenClock,
    make_user: MakeUser,
) -> None:
    settings = make_settings(ATLAS_PUBLIC_URL="https://home.example.net:8443")
    store = SessionStore(
        app_engine,
        settings.session_lifetimes,
        TokenBox(settings.secret_key),
        clock=clock,
    )
    issued, cookie = sign_in(store, make_user, clock)
    client = TestClient(
        build_app(settings, app_engine, clock), base_url="https://home.example.net:8443"
    )
    headers = {**cookie, "X-CSRF-Token": issued.session.csrf_token or ""}
    ok = client.post("/t/change", headers={**headers, "Origin": "https://home.example.net:8443"})
    assert ok.status_code == 200
    refused = client.post("/t/change", headers={**headers, "Origin": "https://home.example.net"})
    assert refused.status_code == 403


# --- step-up ------------------------------------------------------------------------------------


def test_a_fresh_authentication_passes_step_up_and_an_old_one_does_not(
    client: TestClient, store: SessionStore, make_user: MakeUser, clock: FrozenClock
) -> None:
    _, cookie = sign_in(store, make_user, clock)  # auth_time = now
    assert client.get("/t/admin", headers=cookie).status_code == 200
    clock.advance(seconds=300)  # exactly the default: still fine
    assert client.get("/t/admin", headers=cookie).status_code == 200
    clock.advance(seconds=1)
    response = client.get("/t/admin", headers=cookie)
    assert response.status_code == 403
    assert response.json() == {
        "error": {"code": "step_up_required", "message": "Confirm it is you to continue."}
    }
    assert client.get("/t/me", headers=cookie).status_code == 200  # still signed in


def test_step_up_can_name_its_own_limit(
    client: TestClient, store: SessionStore, make_user: MakeUser, clock: FrozenClock
) -> None:
    _, cookie = sign_in(store, make_user, clock)
    clock.advance(seconds=31)
    assert client.get("/t/strict", headers=cookie).status_code == 403
    assert client.get("/t/admin", headers=cookie).status_code == 200  # the default is 300 s


def test_step_up_follows_the_setting(
    make_settings: Callable[..., Settings],
    app_engine: Engine,
    clock: FrozenClock,
    make_user: MakeUser,
) -> None:
    settings = make_settings(ATLAS_STEP_UP_MAX_AGE="60")
    store = SessionStore(
        app_engine, settings.session_lifetimes, TokenBox(settings.secret_key), clock=clock
    )
    _, cookie = sign_in(store, make_user, clock)
    client = TestClient(build_app(settings, app_engine, clock), base_url=PUBLIC)
    clock.advance(seconds=61)
    assert client.get("/t/admin", headers=cookie).status_code == 403


def test_step_up_needs_a_session_first(client: TestClient) -> None:
    assert client.get("/t/admin").status_code == 401


def test_a_step_up_moves_with_the_sessions_auth_time(
    client: TestClient, store: SessionStore, make_user: MakeUser, clock: FrozenClock
) -> None:
    issued, cookie = sign_in(store, make_user, clock)
    clock.advance(seconds=400)
    assert client.get("/t/admin", headers=cookie).status_code == 403
    rotated = store.rotate(issued.session.id, auth_time=clock.now)  # what a step-up does (5c-3)
    assert rotated is not None
    fresh = {"Cookie": f"atlas_session={rotated.token.reveal()}"}
    assert client.get("/t/admin", headers=fresh).status_code == 200


# --- cookies: the __Host- prefix only with Secure -------------------------------------------------


def test_production_uses_host_cookies_and_only_those(
    production: Settings, app_engine: Engine, clock: FrozenClock, make_user: MakeUser
) -> None:
    store = SessionStore(
        app_engine, production.session_lifetimes, TokenBox(production.secret_key), clock=clock
    )
    issued = store.create(make_user(), auth_time=clock.now)
    token = issued.token.reveal()
    client = TestClient(build_app(production, app_engine, clock), base_url=PUBLIC)
    assert production.cookie_secure is True
    assert session_cookie_name(secure=True) == "__Host-atlas_session"
    ok = client.get("/t/me", headers={"Cookie": f"__Host-atlas_session={token}"})
    assert ok.status_code == 200
    # The unprefixed name is not accepted in production: a sibling host could set it.
    plain = client.get("/t/me", headers={"Cookie": f"atlas_session={token}"})
    assert plain.status_code == 401


def test_outside_production_the_cookie_has_no_prefix_and_no_secure(
    settings: Settings,
    client: TestClient,
    store: SessionStore,
    make_user: MakeUser,
    clock: FrozenClock,
) -> None:
    issued = store.create(make_user(), auth_time=clock.now)
    token = issued.token.reveal()
    assert settings.cookie_secure is False
    assert session_cookie_name(secure=False) == "atlas_session"
    assert client.get("/t/me", headers={"Cookie": f"atlas_session={token}"}).status_code == 200
    # A __Host- cookie is not what this deployment sets, so it is not read.
    assert (
        client.get("/t/me", headers={"Cookie": f"__Host-atlas_session={token}"}).status_code == 401
    )


def parse_set_cookie(header: str) -> tuple[str, str, dict[str, str]]:
    name_value, *attrs = [p.strip() for p in header.split(";")]
    name, value = name_value.split("=", 1)
    flags = {a.split("=", 1)[0].lower(): (a.split("=", 1)[1] if "=" in a else "") for a in attrs}
    return name, value, flags


def test_the_production_cookies_have_every_flag(
    production: Settings, app_engine: Engine, clock: FrozenClock
) -> None:
    client = TestClient(build_app(production, app_engine, clock), base_url=PUBLIC)
    headers = client.get("/t/cookies").headers.get_list("set-cookie")
    assert len(headers) == 2
    names = []
    for header in headers:
        name, value, flags = parse_set_cookie(header)
        names.append(name)
        assert name.startswith("__Host-") and len(value) == 43
        assert "secure" in flags and "httponly" in flags
        assert flags["samesite"].lower() == "lax"
        assert flags["path"] == "/"
        assert "domain" not in flags  # a __Host- cookie has none
        assert flags["max-age"] == "60"
    assert names == ["__Host-atlas_session", "__Host-atlas_login"]


def test_the_development_cookies_have_every_flag_but_secure(
    settings: Settings, client: TestClient
) -> None:
    headers = client.get("/t/cookies").headers.get_list("set-cookie")
    assert len(headers) == 2
    for header in headers:
        name, _, flags = parse_set_cookie(header)
        assert not name.startswith("__Host-")
        assert "secure" not in flags
        assert "httponly" in flags and flags["samesite"].lower() == "lax" and flags["path"] == "/"
        assert "domain" not in flags


def test_clearing_a_cookie_keeps_the_same_name_and_flags(
    production: Settings, app_engine: Engine, clock: FrozenClock
) -> None:
    client = TestClient(build_app(production, app_engine, clock), base_url=PUBLIC)
    (header,) = client.get("/t/clear").headers.get_list("set-cookie")
    name, value, flags = parse_set_cookie(header)
    assert name == "__Host-atlas_session" and value in ("", '""')
    assert flags["max-age"] == "0" and flags["path"] == "/"
    assert "secure" in flags and "domain" not in flags


def test_the_name_and_the_flag_cannot_disagree() -> None:
    response = Response()
    with pytest.raises(ValueError, match="__Host-"):
        set_cookie(response, "atlas_session", "v", secure=True, max_age=1)
    with pytest.raises(ValueError, match="must be Secure"):
        set_cookie(response, "__Host-atlas_session", "v", secure=False, max_age=1)
    assert "set-cookie" not in response.headers


# --- nothing secret in a response or a log --------------------------------------------------------


def test_no_secret_in_any_response_or_log(
    client: TestClient,
    store: SessionStore,
    make_user: MakeUser,
    clock: FrozenClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    issued, cookie = sign_in(store, make_user, clock)
    token, csrf = issued.token.reveal(), issued.session.csrf_token or ""
    store.revoke_token(token, "logout")
    stranger = "S" * 42 + "-"
    seen: list[str] = []
    with caplog.at_level(logging.DEBUG):
        for method, path, headers in (
            ("GET", "/t/me", cookie),  # revoked
            ("POST", "/t/change", {**cookie, "X-CSRF-Token": csrf}),  # revoked
            (
                "POST",
                "/t/change",
                {**cookie, "X-CSRF-Token": csrf, "Origin": "https://evil.example.net"},
            ),
            ("GET", "/t/me", {"Cookie": f"atlas_session={stranger}"}),
            ("GET", "/t/admin", cookie),
        ):
            response = client.request(method, path, headers=headers)
            seen.append(response.text + str(dict(response.headers)))
    for secret in (token, csrf, stranger):
        assert all(secret not in text_ for text_ in seen)
        assert secret not in caplog.text


# --- the client address: for limits and the audit log, never for authorization --------------------


def request(peer: str | None, **headers: str) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "query_string": b"",
        "headers": [(k.replace("_", "-").encode(), v.encode()) for k, v in headers.items()],
        "client": (peer, 40000) if peer else None,
    }
    return Request(scope)


@pytest.mark.parametrize(
    "peer", ["127.0.0.1", "10.4.5.6", "172.20.0.9", "192.168.1.5", "::1", "fd00::5"]
)
def test_x_real_ip_is_believed_from_a_loopback_or_private_peer(
    settings: Settings, peer: str
) -> None:
    assert client_address(request(peer, x_real_ip="203.0.113.7"), settings) == "203.0.113.7"
    assert client_address(request(peer, x_real_ip="2001:db8::7"), settings) == "2001:db8::7"


@pytest.mark.parametrize("peer", ["203.0.113.50", "198.51.100.1", "8.8.8.8", "2001:db8::50"])
def test_x_real_ip_is_ignored_from_any_other_peer(settings: Settings, peer: str) -> None:
    assert client_address(request(peer, x_real_ip="192.0.2.99"), settings) == peer


@pytest.mark.parametrize(
    "header", ["", "not-an-address", "203.0.113.7, 198.51.100.2", "999.1.1.1", "\x00"]
)
def test_a_header_that_is_not_an_address_falls_back_to_the_peer(
    settings: Settings, header: str
) -> None:
    assert client_address(request("10.0.0.5", x_real_ip=header), settings) == "10.0.0.5"


def test_no_header_means_the_peer(settings: Settings) -> None:
    assert client_address(request("10.0.0.5"), settings) == "10.0.0.5"


def test_no_peer_means_no_address(settings: Settings) -> None:
    assert client_address(request(None, x_real_ip="203.0.113.7"), settings) is None
    assert client_address(request("testclient", x_real_ip="203.0.113.7"), settings) is None


def test_the_trusted_peers_can_be_narrowed_or_turned_off(
    make_settings: Callable[..., Settings],
) -> None:
    narrow = make_settings(ATLAS_API_TRUSTED_PEERS="172.30.0.0/16")
    assert client_address(request("172.30.1.2", x_real_ip="203.0.113.7"), narrow) == "203.0.113.7"
    assert client_address(request("10.0.0.5", x_real_ip="203.0.113.7"), narrow) == "10.0.0.5"
    nobody = make_settings(ATLAS_API_TRUSTED_PEERS="none")
    assert client_address(request("10.0.0.5", x_real_ip="203.0.113.7"), nobody) == "10.0.0.5"
    assert client_address(request("127.0.0.1", x_real_ip="203.0.113.7"), nobody) == "127.0.0.1"


def test_no_authorization_code_reads_the_client_address() -> None:
    """The rule of ADR-003 §4 as a check: the address is for limits and the audit log only.

    Today nothing but its definition mentions it. A later change that uses it must keep
    it out of the session checks (`current_session`, `require_step_up`).
    """
    import inspect

    from atlas.kernel.web import auth

    for function in (auth.current_session, auth.require_step_up, auth.check_origin, auth.get_store):
        assert "client_address" not in inspect.getsource(function)
        assert "x-real-ip" not in inspect.getsource(function).lower()


# --- the rate limiter is wired to the app ---------------------------------------------------------


def test_the_rate_limiter_is_wired_to_the_app(
    settings: Settings, app_engine: Engine, clock: FrozenClock
) -> None:
    app = create_app(settings, engine=app_engine, clock=clock)

    @app.get("/t/limited")
    def limited(limiter: Annotated[RateLimiter, Depends(get_rate_limiter)]) -> JSONResponse:
        decision = limiter.hit("t:limited", RateSpec(2, 60))
        if not decision.allowed:
            raise ApiError(
                429,
                "rate_limited",
                "Too many requests.",
                {"Retry-After": str(decision.retry_after)},
            )
        return JSONResponse({"ok": True})

    client = TestClient(app, base_url=PUBLIC)
    assert [client.get("/t/limited").status_code for _ in range(3)] == [200, 200, 429]
    refused = client.get("/t/limited")
    assert refused.headers["retry-after"] == "60"
    assert refused.json() == {"error": {"code": "rate_limited", "message": "Too many requests."}}
    clock.advance(seconds=60)  # the next window, by the app's clock
    assert client.get("/t/limited").status_code == 200
