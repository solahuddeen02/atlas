"""The session store (ADR-003 §4): one lookup per request, immediate revocation, fail closed."""

import hashlib
import logging
import pickle
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, event, text

from atlas.kernel.config import Settings
from atlas.kernel.db.ids import uuid7
from atlas.kernel.db.uow import UnitOfWork
from atlas.kernel.identity.admin import Role
from atlas.kernel.identity.sessions import (
    TOKEN_RE,
    IssuedSession,
    Revocation,
    RevokedToken,
    SessionRefused,
    SessionStore,
    SessionToken,
    _live_role,
    end_user_sessions,
    hash_token,
)
from atlas.kernel.identity.tokenbox import TokenBox
from tests.kernel.conftest import FrozenClock, MakeUser, all_text_of_table

REFRESH = "refresh-token-0123456789abcdefghijklmnop"
OLD_KEY = SecretStr("k3Y-ZYXWVUTSRQPONMLKJIHGFEDCBAabcdefghijklmnop-OLD")
NOW = datetime(2026, 11, 19, 12, 0, 0, tzinfo=UTC)


def issue(
    store: SessionStore, user_id: UUID, clock: FrozenClock, **kwargs: object
) -> IssuedSession:
    return store.create(user_id, auth_time=clock.now, **kwargs)  # type: ignore[arg-type]


@contextmanager
def counting(engine: Engine) -> Iterator[list[str]]:
    seen: list[str] = []

    def before(*args: object) -> None:
        seen.append(str(args[2]))

    event.listen(engine, "before_cursor_execute", before)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", before)


def row_of(engine: Engine, session_id: UUID) -> dict[str, object]:
    with engine.connect() as connection:
        row = (
            connection.execute(
                text("SELECT * FROM kernel.sessions WHERE id = :id"), {"id": session_id}
            )
            .mappings()
            .one()
        )
    return dict(row)


# --- creating and finding ---------------------------------------------------------------


def test_a_session_is_found_by_its_cookie_value(
    store: SessionStore, make_user: MakeUser, clock: FrozenClock
) -> None:
    user = make_user(role="adult", username="somchai")
    issued = issue(store, user, clock)
    token = issued.token.reveal()
    assert TOKEN_RE.fullmatch(token)  # 256 bits as 43 base64url characters
    found = store.lookup(token)
    assert found is not None
    assert (found.user_id, found.username, found.role) == (user, "somchai", Role.ADULT)
    assert found.csrf_token and found.csrf_token != token
    assert found.auth_time == clock.now


def test_only_the_hash_of_the_token_is_stored(
    store: SessionStore, app_engine: Engine, make_user: MakeUser, clock: FrozenClock
) -> None:
    issued = issue(store, make_user(), clock, refresh_token=SecretStr(REFRESH))
    token = issued.token.reveal()
    row = row_of(app_engine, issued.session.id)
    assert row["token_hash"] == hashlib.sha256(token.encode()).digest() == hash_token(token)
    everything = all_text_of_table(app_engine, "sessions")
    assert token not in everything
    assert REFRESH not in everything  # the refresh token is stored encrypted
    assert row["idp_tokens_enc"] is not None


def test_each_session_has_its_own_token_and_csrf_token(
    store: SessionStore, make_user: MakeUser, clock: FrozenClock
) -> None:
    user = make_user()
    a, b = issue(store, user, clock), issue(store, user, clock)
    assert a.token.reveal() != b.token.reveal()
    assert a.session.csrf_token != b.session.csrf_token


@pytest.mark.parametrize(
    ("role", "idle", "absolute"),
    [
        ("owner", timedelta(hours=4), timedelta(hours=8)),
        ("admin", timedelta(hours=4), timedelta(hours=8)),
        ("adult", timedelta(hours=12), timedelta(hours=24)),
        ("child", timedelta(hours=12), timedelta(hours=24)),
        ("guest", timedelta(hours=12), timedelta(hours=24)),
    ],
)
def test_the_lifetimes_follow_the_role(
    store: SessionStore,
    make_user: MakeUser,
    clock: FrozenClock,
    role: str,
    idle: timedelta,
    absolute: timedelta,
) -> None:
    session = issue(store, make_user(role=role), clock).session
    assert session.idle_expires_at == clock.now + idle
    assert session.absolute_expires_at == clock.now + absolute


def test_a_session_never_outlasts_the_persons_own_expiry(
    store: SessionStore, make_user: MakeUser, clock: FrozenClock
) -> None:
    user = make_user(role="guest", expires_at=clock.now + timedelta(hours=3))
    issued = issue(store, user, clock)
    assert issued.session.absolute_expires_at == clock.now + timedelta(hours=3)
    assert issued.session.idle_expires_at == clock.now + timedelta(hours=3)
    clock.advance(hours=3)
    assert store.lookup(issued.token.reveal()) is None
    with pytest.raises(SessionRefused):  # and a new one is refused
        issue(store, user, clock)


@pytest.mark.parametrize("make", ["unknown", "disabled", "expired"])
def test_no_session_is_made_for_someone_who_cannot_have_one(
    store: SessionStore, make_user: MakeUser, clock: FrozenClock, make: str
) -> None:
    user = {
        "unknown": uuid7(),
        "disabled": make_user(status="disabled"),
        "expired": make_user(expires_at=clock.now - timedelta(seconds=1)),
    }[make]
    with pytest.raises(SessionRefused):
        issue(store, user, clock)


def test_creating_a_session_ends_the_one_the_browser_presented(
    store: SessionStore, app_engine: Engine, make_user: MakeUser, clock: FrozenClock
) -> None:
    user = make_user()
    old = issue(store, user, clock)
    new = issue(store, user, clock, replaces=old.token.reveal())
    assert store.lookup(old.token.reveal()) is None
    assert store.lookup(new.token.reveal()) is not None
    assert row_of(app_engine, old.session.id)["revoked_reason"] == "replaced"


# --- every request: one lookup, and any doubt means no session ----------------------------


def test_a_request_is_one_query(
    store: SessionStore, app_engine: Engine, make_user: MakeUser, clock: FrozenClock
) -> None:
    token = issue(store, make_user(), clock).token.reveal()
    with counting(app_engine) as statements:
        for _ in range(3):
            clock.advance(seconds=5)
            assert store.lookup(token) is not None
    assert len(statements) == 3  # one SELECT each; no write inside the touch interval
    assert all(s.lstrip().upper().startswith("SELECT") for s in statements)
    assert all("JOIN kernel.users" in s for s in statements)


def test_a_malformed_cookie_costs_no_query(store: SessionStore, app_engine: Engine) -> None:
    with counting(app_engine) as statements:
        for bad in (
            "",
            "short",
            "A" * 42,
            "A" * 44,
            "A" * 42 + "!",
            "A" * 42 + "=",
            " " + "A" * 42,
        ):
            assert store.lookup(bad) is None
        assert store.lookup(None) is None  # type: ignore[arg-type]
    assert statements == []


def test_an_unknown_token_is_no_session(store: SessionStore) -> None:
    assert store.lookup("A" * 43) is None


def test_the_last_seen_time_is_written_at_most_once_a_minute(
    store: SessionStore, app_engine: Engine, make_user: MakeUser, clock: FrozenClock
) -> None:
    issued = issue(store, make_user(), clock)
    token, started = issued.token.reveal(), clock.now
    clock.advance(seconds=59)
    with counting(app_engine) as statements:
        assert store.lookup(token) is not None
    assert len(statements) == 1
    assert row_of(app_engine, issued.session.id)["last_seen_at"] == started
    clock.advance(seconds=2)  # 61 s since the last write
    with counting(app_engine) as statements:
        found = store.lookup(token)
    assert found is not None and found.last_seen_at == clock.now
    assert len(statements) == 2  # the lookup, then the write
    assert row_of(app_engine, issued.session.id)["last_seen_at"] == clock.now


def test_the_idle_time_slides_but_never_past_the_absolute_time(
    store: SessionStore, make_user: MakeUser, clock: FrozenClock
) -> None:
    issued = issue(store, make_user(role="adult"), clock)
    token, started = issued.token.reveal(), clock.now
    clock.advance(hours=11)  # inside the 12 h idle time
    found = store.lookup(token)
    assert found is not None and found.idle_expires_at == clock.now + timedelta(hours=12)
    clock.advance(hours=11)  # 22 h in: idle again would be 34 h, but the day ends at 24 h
    found = store.lookup(token)
    assert found is not None
    assert found.idle_expires_at == started + timedelta(hours=24) == found.absolute_expires_at
    clock.advance(hours=2, minutes=1)  # past the absolute time
    assert store.lookup(token) is None


def test_a_session_that_nobody_used_ends_at_its_idle_time(
    store: SessionStore, make_user: MakeUser, clock: FrozenClock
) -> None:
    token = issue(store, make_user(role="owner"), clock).token.reveal()  # idle 4 h
    clock.advance(hours=4)
    assert store.lookup(token) is None
    assert store.lookup(token) is None  # and stays gone


def test_the_absolute_time_ends_a_session_that_is_always_in_use(
    store: SessionStore, make_user: MakeUser, clock: FrozenClock
) -> None:
    token = issue(store, make_user(role="owner"), clock).token.reveal()  # absolute 8 h
    for _ in range(7):
        clock.advance(hours=1)
        assert store.lookup(token) is not None
    clock.advance(hours=1)
    assert store.lookup(token) is None


def test_a_revoked_session_is_gone_on_the_next_request(
    store: SessionStore, app_engine: Engine, make_user: MakeUser, clock: FrozenClock
) -> None:
    issued = issue(store, make_user(), clock)
    token = issued.token.reveal()
    assert store.lookup(token) is not None
    assert store.revoke(issued.session.id, "admin").ended == 1
    assert store.lookup(token) is None
    assert store.revoke(issued.session.id, "admin").ended == 0  # already ended
    row = row_of(app_engine, issued.session.id)
    assert row["revoked_reason"] == "admin" and row["revoked_at"] == clock.now


def test_logout_revokes_by_cookie_value(
    store: SessionStore, make_user: MakeUser, clock: FrozenClock
) -> None:
    token = issue(store, make_user(), clock).token.reveal()
    assert store.revoke_token(token, "logout").ended == 1
    assert store.lookup(token) is None
    assert store.revoke_token(token, "logout").ended == 0
    assert store.revoke_token("not a token", "logout").ended == 0


def test_a_reason_must_be_one_the_database_knows(
    store: SessionStore, make_user: MakeUser, clock: FrozenClock
) -> None:
    issued = issue(store, make_user(), clock)
    with pytest.raises(ValueError, match="unknown revoke reason"):
        store.revoke(issued.session.id, "because")
    with pytest.raises(ValueError, match="unknown revoke reason"):
        store.revoke_token(issued.token.reveal(), "because")
    with pytest.raises(ValueError, match="unknown revoke reason"):
        store.end_user_sessions(issued.session.user_id, "because")
    assert store.lookup(issued.token.reveal()) is not None


def test_a_person_who_is_disabled_has_no_session(
    store: SessionStore, app_engine: Engine, make_user: MakeUser, clock: FrozenClock
) -> None:
    user = make_user()
    token = issue(store, user, clock).token.reveal()
    with app_engine.begin() as connection:
        connection.execute(
            text("UPDATE kernel.users SET status = 'disabled' WHERE id = :u"), {"u": user}
        )
    assert store.lookup(token) is None  # no revocation was needed: the person is not active


def test_a_person_whose_time_is_up_has_no_session(
    store: SessionStore, app_engine: Engine, make_user: MakeUser, clock: FrozenClock
) -> None:
    user = make_user(role="guest", expires_at=clock.now + timedelta(hours=2))
    token = issue(store, user, clock).token.reveal()
    clock.advance(hours=1)
    assert store.lookup(token) is not None
    # The expiry is moved earlier (an admin shortens a guest's visit): the session follows.
    with app_engine.begin() as connection:
        connection.execute(
            text("UPDATE kernel.users SET expires_at = :t WHERE id = :u"),
            {"t": clock.now - timedelta(seconds=1), "u": user},
        )
    assert store.lookup(token) is None


def test_a_deleted_person_takes_their_sessions_with_them(
    store: SessionStore, app_engine: Engine, make_user: MakeUser, clock: FrozenClock
) -> None:
    user = make_user()
    token = issue(store, user, clock).token.reveal()
    with app_engine.begin() as connection:
        connection.execute(text("DELETE FROM kernel.users WHERE id = :u"), {"u": user})
    assert store.lookup(token) is None


def test_a_change_of_role_is_followed_on_the_next_request(
    store: SessionStore, app_engine: Engine, make_user: MakeUser, clock: FrozenClock
) -> None:
    user = make_user(role="adult")
    token = issue(store, user, clock).token.reveal()
    found = store.lookup(token)
    assert found is not None and found.role is Role.ADULT
    with app_engine.begin() as connection:
        connection.execute(
            text("UPDATE kernel.users SET role = 'admin' WHERE id = :u"), {"u": user}
        )
    found = store.lookup(token)
    assert found is not None and found.role is Role.ADMIN


def test_a_role_the_store_does_not_know_is_no_session() -> None:
    # The database's CHECK forbids such a role, so exercise the rule on a row of our own.
    row = SimpleNamespace(
        revoked_at=None,
        idle_expires_at=NOW + timedelta(hours=1),
        absolute_expires_at=NOW + timedelta(hours=1),
        user_status="active",
        user_expires_at=None,
        user_role="root",
    )
    assert _live_role(row, NOW) is None  # type: ignore[arg-type]
    row.user_role = "adult"
    assert _live_role(row, NOW) is Role.ADULT  # type: ignore[arg-type]


def test_every_doubt_fails_closed(
    store: SessionStore, app_engine: Engine, make_user: MakeUser, clock: FrozenClock
) -> None:
    """Each condition on its own ends the session; with none of them it is live."""
    user = make_user()
    past = {"past": clock.now - timedelta(seconds=1)}
    cases: dict[str, tuple[str, dict[str, object]]] = {
        "revoked": (
            "UPDATE kernel.sessions SET revoked_at = now(), revoked_reason = 'user'",
            {},
        ),
        "idle over": ("UPDATE kernel.sessions SET idle_expires_at = :past", past),
        "absolute over": ("UPDATE kernel.sessions SET absolute_expires_at = :past", past),
    }
    for name, (sql, params) in cases.items():
        token = issue(store, user, clock).token.reveal()
        with app_engine.begin() as connection:
            connection.execute(text(sql), params)
        assert store.lookup(token) is None, name
        with app_engine.begin() as connection:
            connection.execute(text("DELETE FROM kernel.sessions"))
    token = issue(store, user, clock).token.reveal()
    assert store.lookup(token) is not None


# --- ending sessions together with the change that needs it -------------------------------


def test_all_of_a_persons_sessions_end_and_nobody_elses(
    store: SessionStore, make_user: MakeUser, clock: FrozenClock
) -> None:
    alice, bob = make_user(), make_user()
    a1, a2 = issue(store, alice, clock), issue(store, alice, clock)
    b1 = issue(store, bob, clock)
    gone = issue(store, alice, clock)
    store.revoke(gone.session.id, "logout")
    assert store.end_user_sessions(alice, "role_changed").ended == 2  # not the revoked one
    assert store.lookup(a1.token.reveal()) is None and store.lookup(a2.token.reveal()) is None
    assert store.lookup(b1.token.reveal()) is not None
    assert store.end_user_sessions(alice, "role_changed").ended == 0


def test_the_revocation_commits_with_the_change_or_not_at_all(
    store: SessionStore,
    app_engine: Engine,
    box: TokenBox,
    make_user: MakeUser,
    clock: FrozenClock,
) -> None:
    user = make_user()
    token = issue(store, user, clock).token.reveal()

    with pytest.raises(RuntimeError, match="boom"), UnitOfWork(app_engine) as db:
        db.execute(text("UPDATE kernel.users SET status = 'disabled' WHERE id = :u"), {"u": user})
        end_user_sessions(db, user, "disabled", box=box, now=clock.now)
        raise RuntimeError("boom")
    assert store.lookup(token) is not None  # rolled back together: still signed in, still active

    with UnitOfWork(app_engine) as db:
        db.execute(text("UPDATE kernel.users SET status = 'disabled' WHERE id = :u"), {"u": user})
        assert end_user_sessions(db, user, "disabled", box=box, now=clock.now).ended == 1
    assert store.lookup(token) is None


def test_rotation_replaces_the_cookie_value_and_the_csrf_token(
    store: SessionStore, app_engine: Engine, make_user: MakeUser, clock: FrozenClock
) -> None:
    issued = issue(store, make_user(), clock)
    old_token = issued.token.reveal()
    clock.advance(minutes=3)
    rotated = store.rotate(issued.session.id, auth_time=clock.now)
    assert rotated is not None
    new_token = rotated.token.reveal()
    assert new_token != old_token
    assert store.lookup(old_token) is None  # the old cookie stops working at once
    found = store.lookup(new_token)
    assert found is not None and found.id == issued.session.id  # the same session
    assert found.csrf_token != issued.session.csrf_token
    assert found.auth_time == clock.now  # a step-up moves it forward
    assert row_of(app_engine, issued.session.id)["token_hash"] == hash_token(new_token)


def test_a_session_that_is_over_cannot_be_rotated(
    store: SessionStore, make_user: MakeUser, clock: FrozenClock
) -> None:
    issued = issue(store, make_user(), clock)
    store.revoke(issued.session.id, "user")
    assert store.rotate(issued.session.id) is None


# --- the stored IdP token ---------------------------------------------------------------------


def test_the_refresh_token_is_taken_once_and_leaves_the_row(
    store: SessionStore, app_engine: Engine, make_user: MakeUser, clock: FrozenClock
) -> None:
    issued = issue(store, make_user(), clock, refresh_token=SecretStr(REFRESH))
    taken = store.take_refresh_token(issued.session.id)
    assert taken is not None and taken.get_secret_value() == REFRESH
    assert row_of(app_engine, issued.session.id)["idp_tokens_enc"] is None
    assert store.take_refresh_token(issued.session.id) is None


def test_a_session_without_a_refresh_token_has_none_to_take(
    store: SessionStore, make_user: MakeUser, clock: FrozenClock
) -> None:
    assert store.take_refresh_token(issue(store, make_user(), clock).session.id) is None
    assert store.take_refresh_token(uuid7()) is None


def test_a_stored_token_copied_to_another_row_cannot_be_read(
    store: SessionStore,
    app_engine: Engine,
    make_user: MakeUser,
    clock: FrozenClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    user = make_user()
    a = issue(store, user, clock, refresh_token=SecretStr(REFRESH))
    b = issue(store, user, clock, refresh_token=SecretStr("another-refresh-token-value-0123456789"))
    with app_engine.begin() as connection:  # an attacker with write access to the table
        connection.execute(
            text(
                "UPDATE kernel.sessions SET idp_tokens_enc = "
                "(SELECT idp_tokens_enc FROM kernel.sessions WHERE id = :a) WHERE id = :b"
            ),
            {"a": a.session.id, "b": b.session.id},
        )
    with caplog.at_level(logging.DEBUG):
        assert store.take_refresh_token(b.session.id) is None
    assert REFRESH not in caplog.text


def test_a_token_stored_under_the_previous_key_is_still_read(
    app_engine: Engine, make_user: MakeUser, clock: FrozenClock, settings: Settings
) -> None:
    user = make_user()
    old = SessionStore(app_engine, settings.session_lifetimes, TokenBox(OLD_KEY), clock=clock)
    issued = issue(old, user, clock, refresh_token=SecretStr(REFRESH))
    rotated = SessionStore(
        app_engine,
        settings.session_lifetimes,
        TokenBox(settings.secret_key, previous=OLD_KEY),
        clock=clock,
    )
    taken = rotated.take_refresh_token(issued.session.id)
    assert taken is not None and taken.get_secret_value() == REFRESH


# --- nothing secret in a repr, an error or a log ------------------------------------------------


def test_nothing_secret_shows_in_a_repr(
    store: SessionStore, make_user: MakeUser, clock: FrozenClock
) -> None:
    issued = issue(store, make_user(), clock, refresh_token=SecretStr(REFRESH))
    token, csrf = issued.token.reveal(), issued.session.csrf_token
    assert csrf
    found = store.lookup(token)
    assert found is not None
    objects = [issued, issued.session, issued.token, found, store, SessionToken(token)]
    for obj in objects:
        for text_ in (repr(obj), str(obj), f"{obj}", f"{obj!r}"):
            assert token not in text_ and csrf not in text_ and REFRESH not in text_
            assert hash_token(token).hex() not in text_
    assert "redacted" in repr(issued.token)
    assert "role=adult" in repr(issued.session)


def test_sessions_and_tokens_are_not_serialised(
    store: SessionStore, make_user: MakeUser, clock: FrozenClock
) -> None:
    issued = issue(store, make_user(), clock)
    for obj in (issued.token, issued.session):
        with pytest.raises(TypeError, match="not serialised"):
            pickle.dumps(obj)


def test_nothing_secret_is_logged(
    store: SessionStore,
    make_user: MakeUser,
    clock: FrozenClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    user = make_user()
    with caplog.at_level(logging.DEBUG):
        issued = issue(store, user, clock, refresh_token=SecretStr(REFRESH))
        token, csrf = issued.token.reveal(), issued.session.csrf_token
        assert csrf
        clock.advance(minutes=5)
        store.lookup(token)
        store.lookup("A" * 43)
        rotated = store.rotate(issued.session.id)
        assert rotated is not None
        store.revoke_token(rotated.token.reveal(), "logout")
        store.end_user_sessions(user, "disabled")
        store.take_refresh_token(issued.session.id)
        store.purge()
    for secret in (token, csrf, REFRESH, rotated.token.reveal(), rotated.session.csrf_token):
        assert secret and secret not in caplog.text


# --- housekeeping -----------------------------------------------------------------------------


def test_purge_removes_old_sessions_and_logins_and_keeps_the_rest(
    store: SessionStore,
    app_engine: Engine,
    make_user: MakeUser,
    clock: FrozenClock,
) -> None:
    user = make_user()
    expired = issue(store, user, clock)  # absolute time passes long ago
    revoked = issue(store, user, clock)
    store.revoke(revoked.session.id, "logout")
    clock.advance(days=10)
    live = issue(store, user, clock)
    recently_revoked = issue(store, user, clock)
    store.revoke(recently_revoked.session.id, "logout")
    with app_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO kernel.login_attempts (id, cookie_hash, state_hash, nonce, "
                "code_verifier_enc, purpose, expires_at) VALUES "
                "(:a, '\\x01', '\\x01', 'n', '\\x01', 'login', :past), "
                "(:b, '\\x02', '\\x02', 'n', '\\x02', 'login', :future)"
            ),
            {
                "a": uuid7(),
                "b": uuid7(),
                "past": clock.now - timedelta(minutes=1),
                "future": clock.now + timedelta(minutes=5),
            },
        )
    assert store.purge() == 2  # the expired one and the old revoked one
    with app_engine.connect() as connection:
        ids = {r[0] for r in connection.execute(text("SELECT id FROM kernel.sessions"))}
        logins = connection.execute(text("SELECT count(*) FROM kernel.login_attempts")).scalar_one()
    assert ids == {live.session.id, recently_revoked.session.id}
    assert expired.session.id not in ids and revoked.session.id not in ids
    assert logins == 1


def test_the_address_and_the_user_agent_are_stored_cleaned(
    store: SessionStore, app_engine: Engine, make_user: MakeUser, clock: FrozenClock
) -> None:
    user = make_user()
    agent = (
        "Mozilla/5.0 " + chr(0) + chr(27) + "[31m(X11)" + chr(10) + "Injected: line " + "x" * 400
    )
    issued = issue(store, user, clock, client_ip="203.0.113.7", user_agent=agent)
    row = row_of(app_engine, issued.session.id)
    assert str(row["client_ip"]) == "203.0.113.7"
    stored = str(row["user_agent"])
    assert len(stored) <= 256
    assert not any(ord(c) < 32 or ord(c) == 127 for c in stored)
    assert stored.startswith("Mozilla/5.0 ")

    v6 = issue(store, user, clock, client_ip=" 2001:db8::1 ")
    assert str(row_of(app_engine, v6.session.id)["client_ip"]) == "2001:db8::1"
    for junk in ("not an address", "203.0.113.7; DROP TABLE kernel.sessions", "999.1.1.1", ""):
        issued = issue(store, user, clock, client_ip=junk, user_agent="")
        row = row_of(app_engine, issued.session.id)
        assert row["client_ip"] is None and row["user_agent"] is None


# --- a revoked session keeps no IdP token, and hands it back (R1) ---------------------------------

OTHER = "another-refresh-token-9876543210zyxwvutsrqponm"


def test_ending_a_session_clears_its_token_in_the_same_statement(
    store: SessionStore, app_engine: Engine, make_user: MakeUser, clock: FrozenClock
) -> None:
    issued = issue(store, make_user(), clock, refresh_token=SecretStr(REFRESH))
    assert row_of(app_engine, issued.session.id)["idp_tokens_enc"] is not None
    with counting(app_engine) as statements:
        revocation = store.revoke(issued.session.id, "admin")
    assert len(statements) == 1  # one statement: the CTE reads the token, the UPDATE clears it
    assert statements[0].lstrip().upper().startswith("WITH")
    assert "UPDATE kernel.sessions" in statements[0] and "idp_tokens_enc" in statements[0]
    row = row_of(app_engine, issued.session.id)
    assert row["idp_tokens_enc"] is None
    assert row["revoked_at"] == clock.now and row["revoked_reason"] == "admin"
    assert revocation.ended == 1
    (token,) = revocation.tokens
    assert token.session_id == issued.session.id
    assert isinstance(token.refresh_token, SecretStr)
    assert token.refresh_token.get_secret_value() == REFRESH


@pytest.mark.parametrize(
    "kind", ["revoke", "revoke_token", "end_user_sessions", "in_a_transaction", "replaced_by_login"]
)
def test_after_each_kind_of_revocation_the_row_holds_no_token(
    store: SessionStore,
    app_engine: Engine,
    box: TokenBox,
    make_user: MakeUser,
    clock: FrozenClock,
    kind: str,
) -> None:
    user = make_user()
    first = issue(store, user, clock, refresh_token=SecretStr(REFRESH))
    expected = {first.session.id: REFRESH}
    survivor: IssuedSession | None = None
    if kind == "revoke":
        result = store.revoke(first.session.id, "admin")
    elif kind == "revoke_token":
        result = store.revoke_token(first.token.reveal(), "logout")
    elif kind == "replaced_by_login":
        survivor = issue(
            store, user, clock, replaces=first.token.reveal(), refresh_token=SecretStr(OTHER)
        )
        result = survivor.replaced
    else:
        second = issue(store, user, clock, refresh_token=SecretStr(OTHER))
        expected[second.session.id] = OTHER
        if kind == "end_user_sessions":
            result = store.end_user_sessions(user, "disabled")
        else:
            with UnitOfWork(app_engine) as db:
                result = end_user_sessions(db, user, "disabled", box=box, now=clock.now)

    # What came back is exactly what was stored, per session...
    assert result.ended == len(expected)
    assert {t.session_id: t.refresh_token.get_secret_value() for t in result.tokens} == expected
    # ...and the rows hold nothing of it any more.
    for session_id in expected:
        row = row_of(app_engine, session_id)
        assert row["idp_tokens_enc"] is None and row["revoked_at"] is not None
        assert store.take_refresh_token(session_id) is None
    for plain in expected.values():
        assert plain not in all_text_of_table(app_engine, "sessions")
    if survivor is not None:  # the new session of a login keeps its own token
        assert row_of(app_engine, survivor.session.id)["idp_tokens_enc"] is not None
        assert survivor.session.id not in expected


def test_a_token_is_handed_back_once(
    store: SessionStore, make_user: MakeUser, clock: FrozenClock
) -> None:
    issued = issue(store, make_user(), clock, refresh_token=SecretStr(REFRESH))
    assert len(store.revoke(issued.session.id, "user").tokens) == 1
    again = store.revoke(issued.session.id, "user")
    assert (again.ended, again.tokens) == (0, ())
    assert store.revoke_token(issued.token.reveal(), "user").tokens == ()
    assert store.end_user_sessions(issued.session.user_id, "user").tokens == ()


def test_a_session_without_a_token_is_ended_with_nothing_to_hand_back(
    store: SessionStore, make_user: MakeUser, clock: FrozenClock
) -> None:
    issued = issue(store, make_user(), clock)
    result = store.revoke(issued.session.id, "user")
    assert (result.ended, result.tokens) == (1, ())


def test_only_the_persons_own_sessions_are_touched(
    store: SessionStore, app_engine: Engine, make_user: MakeUser, clock: FrozenClock
) -> None:
    alice, bob = make_user(), make_user()
    issue(store, alice, clock, refresh_token=SecretStr(REFRESH))
    bobs = issue(store, bob, clock, refresh_token=SecretStr(OTHER))
    result = store.end_user_sessions(alice, "role_changed")
    assert [t.refresh_token.get_secret_value() for t in result.tokens] == [REFRESH]
    assert row_of(app_engine, bobs.session.id)["idp_tokens_enc"] is not None
    assert store.lookup(bobs.token.reveal()) is not None


def test_the_clearing_is_rolled_back_with_the_callers_transaction(
    store: SessionStore, app_engine: Engine, box: TokenBox, make_user: MakeUser, clock: FrozenClock
) -> None:
    issued = issue(store, make_user(), clock, refresh_token=SecretStr(REFRESH))
    with pytest.raises(RuntimeError, match="boom"), UnitOfWork(app_engine) as db:
        end_user_sessions(db, issued.session.user_id, "disabled", box=box, now=clock.now)
        raise RuntimeError("boom")
    row = row_of(app_engine, issued.session.id)
    assert row["idp_tokens_enc"] is not None and row["revoked_at"] is None  # nothing happened
    taken = store.take_refresh_token(issued.session.id)
    assert taken is not None and taken.get_secret_value() == REFRESH


def test_a_token_that_cannot_be_read_is_cleared_all_the_same(
    app_engine: Engine,
    make_user: MakeUser,
    clock: FrozenClock,
    settings: Settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    user = make_user()
    old = SessionStore(app_engine, settings.session_lifetimes, TokenBox(OLD_KEY), clock=clock)
    issued = issue(old, user, clock, refresh_token=SecretStr(REFRESH))
    current = SessionStore(
        app_engine, settings.session_lifetimes, TokenBox(settings.secret_key), clock=clock
    )
    with caplog.at_level(logging.DEBUG):
        result = current.revoke(issued.session.id, "admin")
    assert (result.ended, result.tokens) == (1, ())  # ended, and nothing to hand back
    assert row_of(app_engine, issued.session.id)["idp_tokens_enc"] is None
    assert "could not be read" in caplog.text and REFRESH not in caplog.text
    # With the previous key configured the same session would have handed it back.
    other = issue(old, user, clock, refresh_token=SecretStr(OTHER))
    rotated = SessionStore(
        app_engine,
        settings.session_lifetimes,
        TokenBox(settings.secret_key, previous=OLD_KEY),
        clock=clock,
    )
    (token,) = rotated.revoke(other.session.id, "admin").tokens
    assert token.refresh_token.get_secret_value() == OTHER


def test_parallel_revocations_hand_a_token_back_exactly_once(
    store: SessionStore, make_user: MakeUser, clock: FrozenClock
) -> None:
    issued = issue(store, make_user(), clock, refresh_token=SecretStr(REFRESH))
    workers = 8
    barrier = threading.Barrier(workers)

    def revoke(_: int) -> Revocation:
        barrier.wait()
        return store.revoke(issued.session.id, "user")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(revoke, range(workers)))
    assert sum(r.ended for r in results) == 1
    tokens = [t for r in results for t in r.tokens]
    assert [t.refresh_token.get_secret_value() for t in tokens] == [REFRESH]


def test_nothing_of_a_returned_token_is_logged_or_shown(
    store: SessionStore,
    make_user: MakeUser,
    clock: FrozenClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    user = make_user()
    with caplog.at_level(logging.DEBUG):
        a = issue(store, user, clock, refresh_token=SecretStr(REFRESH))
        b = issue(store, user, clock, refresh_token=SecretStr(OTHER))
        c = issue(
            store, user, clock, refresh_token=SecretStr("third-refresh-token-abcdefghij0123456789")
        )
        results = [
            store.revoke(a.session.id, "admin"),
            store.revoke_token(b.token.reveal(), "logout"),
            store.end_user_sessions(user, "disabled"),
        ]
        replaced = issue(store, user, clock, replaces=c.token.reveal())
    for result in [*results, replaced.replaced]:
        for obj in (result, *result.tokens):
            for text_ in (repr(obj), str(obj), f"{obj}", f"{obj!r}"):
                assert REFRESH not in text_ and OTHER not in text_
        for token in result.tokens:
            assert "redacted" in repr(token)
            with pytest.raises(TypeError, match="not serialised"):
                pickle.dumps(token)
    assert isinstance(results[0].tokens[0], RevokedToken)
    for secret in (REFRESH, OTHER, "third-refresh-token-abcdefghij0123456789"):
        assert secret not in caplog.text
