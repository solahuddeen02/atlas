"""Server-side sessions (ADR-003 §4).

A session is a row in `kernel.sessions`. The cookie holds 256 random bits; the row holds
only their SHA-256, so a copy of the database cannot be turned into a cookie.

**Every request is one lookup by that hash, joined to the person.** The session counts
only if all of these hold, and any doubt means "no session":
not revoked, not past its idle time, not past its absolute time, the person is `active`
and not past their own `expires_at`, and the stored role is one Atlas knows. Nothing is
cached: a revocation is in force on the next request. (Keeping `last_seen_at` current is a
write at most once per `TOUCH_INTERVAL`, not a second lookup.)

Disabling a person, changing their role and a guest's expiry end their sessions through
`end_user_sessions`, which can run inside the caller's own transaction so that the change
and the revocation commit together.

**A revoked session keeps no IdP token.** Kanidm revives a refresh token when an account is
enabled again, so a revoked row must not hold one. Every way of ending a session (`revoke`,
`revoke_token`, `end_user_sessions`, and `create(replaces=...)`) sets `revoked_at` and clears
`idp_tokens_enc` in the same statement, and hands back what it cleared as a `Revocation`
(session id and the decrypted refresh token). The caller revokes those tokens at the IdP
after its own commit (Phase 5c-3).

Nothing here puts a token, a CSRF token or a stored IdP token in a log line, an error or a
`repr`: `Session` and `SessionToken` show only ids and the role.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any, cast
from uuid import UUID

from pydantic import SecretStr
from sqlalchemy import Connection, Engine, Result, Row, Select, delete, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.sql.elements import ColumnElement

from atlas.kernel.config import SessionLifetimes
from atlas.kernel.db.ids import uuid7
from atlas.kernel.db.metadata import login_attempts, sessions, users
from atlas.kernel.db.uow import UnitOfWork
from atlas.kernel.identity.admin import Role
from atlas.kernel.identity.tokenbox import Purpose, TokenBox, TokenBoxError

logger = logging.getLogger("atlas.sessions")

# 32 random bytes, base64url without padding.
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
TOKEN_BYTES = 32
TOUCH_INTERVAL = timedelta(seconds=60)
USER_AGENT_MAX = 256
# How long a revoked or expired session row is kept (for "where was I signed in").
DEFAULT_RETENTION = timedelta(days=7)

# Why a session was ended. The database allows exactly these (ck_sessions_revoked_reason).
REVOKE_REASONS = frozenset(
    {
        "logout",
        "user",
        "admin",
        "disabled",
        "role_changed",
        "expired",
        "idp_ended",
        "idp_unreachable",
        "replaced",
    }
)


class SessionRefused(Exception):
    """A session cannot be made for this person (unknown, disabled or expired)."""

    def __init__(self) -> None:
        super().__init__("a session cannot be created for this person")


class SessionToken:
    """The cookie value of a session. Shows nothing of itself except through `reveal()`."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        """The cookie value: for `Set-Cookie` and for nothing else. Never log it."""
        return self._value

    def __repr__(self) -> str:
        return "SessionToken(<redacted>)"

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        return format(repr(self), spec)

    def __reduce__(self) -> str | tuple[object, ...]:
        raise TypeError("a SessionToken is not serialised")


@dataclass(frozen=True, repr=False)
class Session:
    """A live session, as the request path sees it. `repr` is redacted."""

    id: UUID
    user_id: UUID
    username: str
    display_name: str
    role: Role
    auth_time: datetime
    created_at: datetime
    last_seen_at: datetime
    idle_expires_at: datetime
    absolute_expires_at: datetime
    # The synchronizer token the SPA sends back as X-CSRF-Token. Secret: never in a repr.
    csrf_token: str

    def __repr__(self) -> str:
        return f"Session(user_id={self.user_id}, role={self.role.value})"

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        return format(repr(self), spec)

    def __reduce__(self) -> str | tuple[object, ...]:
        raise TypeError("a Session is not serialised")


@dataclass(frozen=True, repr=False)
class RevokedToken:
    """The IdP refresh token a revoked session held, to be revoked at the IdP. Redacted."""

    session_id: UUID
    refresh_token: SecretStr

    def __repr__(self) -> str:
        return f"RevokedToken(session_id={self.session_id}, refresh_token=<redacted>)"

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        return format(repr(self), spec)

    def __reduce__(self) -> str | tuple[object, ...]:
        raise TypeError("a RevokedToken is not serialised")


@dataclass(frozen=True, repr=False)
class Revocation:
    """What ending sessions did: how many it ended, and the IdP tokens it cleared.

    `tokens` has an entry for each ended session that held a token Atlas could read. The
    rows no longer hold them, so these are the only copy: revoke them at the IdP.
    """

    ended: int
    tokens: tuple[RevokedToken, ...] = ()

    def __repr__(self) -> str:
        return f"Revocation(ended={self.ended}, tokens={len(self.tokens)})"

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        return format(repr(self), spec)


@dataclass(frozen=True, repr=False)
class IssuedSession:
    """A new (or rotated) session and the cookie value that goes with it.

    `replaced` is what ending the browser's previous session (`create(replaces=...)`) cleared.
    """

    session: Session
    token: SessionToken
    replaced: Revocation = field(default_factory=lambda: Revocation(0))

    def __repr__(self) -> str:
        return f"IssuedSession({self.session!r}, token=<redacted>)"

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        return format(repr(self), spec)


def new_token() -> str:
    """256 random bits, as 43 base64url characters."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> bytes:
    return sha256(token.encode("ascii")).digest()


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _clean_user_agent(value: str | None) -> str | None:
    if not value:
        return None
    text = "".join(c for c in value if c.isprintable())[:USER_AGENT_MAX]
    return text or None


def _clean_address(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return None


def _rows(result: Result[Any]) -> int:
    """How many rows an UPDATE or DELETE touched."""
    return int(cast("CursorResult[Any]", result).rowcount)


def _check_reason(reason: str) -> None:
    if reason not in REVOKE_REASONS:
        raise ValueError(f"unknown revoke reason: {reason!r}")


def _revoke(
    db: DbSession | Connection,
    where: ColumnElement[bool],
    reason: str,
    now: datetime,
    box: TokenBox,
) -> Revocation:
    """End the live sessions matching `where`, in ONE statement that also clears their tokens.

    A CTE locks and reads the rows (with the token as it was), the UPDATE sets `revoked_at`,
    the reason and `idp_tokens_enc = NULL`, and RETURNING hands back the old token. A token
    Atlas cannot read (a `SECRET_KEY` that no longer opens it) is cleared all the same and is
    simply not returned.
    """
    _check_reason(reason)
    old = (
        select(sessions.c.id, sessions.c.idp_tokens_enc.label("old_tokens"))
        .where(where, sessions.c.revoked_at.is_(None))
        .with_for_update()
        .cte("old")
    )
    rows = db.execute(
        update(sessions)
        .where(sessions.c.id == old.c.id)
        .values(revoked_at=now, revoked_reason=reason, idp_tokens_enc=None)
        .returning(sessions.c.id, old.c.old_tokens)
    ).all()
    tokens: list[RevokedToken] = []
    for row in rows:
        if row.old_tokens is None:
            continue
        try:
            plain = box.open(Purpose.REFRESH_TOKEN, row.id, bytes(row.old_tokens))
        except TokenBoxError:
            logger.warning("a stored IdP token could not be read for session %s", row.id)
            continue
        tokens.append(RevokedToken(session_id=row.id, refresh_token=SecretStr(plain.decode())))
    return Revocation(ended=len(rows), tokens=tuple(tokens))


def end_user_sessions(
    db: DbSession | Connection,
    user_id: UUID,
    reason: str,
    *,
    box: TokenBox,
    now: datetime | None = None,
) -> Revocation:
    """End every live session of `user_id`. Returns how many it ended and the tokens it cleared.

    `db` is the caller's own transaction, so that the change that makes this necessary (the
    person is disabled, their role changes, a guest's time is up) and the revocation commit
    together, or not at all. The caller revokes the returned tokens at the IdP once it has
    committed.
    """
    return _revoke(db, sessions.c.user_id == user_id, reason, now or _utc_now(), box)


def _joined_select() -> Select[Any]:
    return select(
        sessions.c.id,
        sessions.c.user_id,
        sessions.c.auth_time,
        sessions.c.created_at,
        sessions.c.last_seen_at,
        sessions.c.idle_expires_at,
        sessions.c.absolute_expires_at,
        sessions.c.revoked_at,
        sessions.c.csrf_token,
        users.c.username,
        users.c.display_name,
        users.c.role.label("user_role"),
        users.c.status.label("user_status"),
        users.c.expires_at.label("user_expires_at"),
    ).select_from(sessions.join(users, users.c.id == sessions.c.user_id))


def _live_role(row: Row[Any], now: datetime) -> Role | None:
    """The person's role if every condition holds, else None (any doubt: no session)."""
    if row.revoked_at is not None:
        return None
    if not (row.idle_expires_at > now and row.absolute_expires_at > now):
        return None
    if row.user_status != "active":
        return None
    if row.user_expires_at is not None and row.user_expires_at <= now:
        return None
    try:
        return Role(row.user_role)
    except ValueError:
        return None


def _to_session(row: Row[Any], role: Role) -> Session:
    return Session(
        id=row.id,
        user_id=row.user_id,
        username=row.username,
        display_name=row.display_name,
        role=role,
        auth_time=row.auth_time,
        created_at=row.created_at,
        last_seen_at=row.last_seen_at,
        idle_expires_at=row.idle_expires_at,
        absolute_expires_at=row.absolute_expires_at,
        csrf_token=row.csrf_token,
    )


class SessionStore:
    def __init__(
        self,
        engine: Engine,
        lifetimes: Callable[[str], SessionLifetimes],
        tokenbox: TokenBox,
        *,
        clock: Callable[[], datetime] = _utc_now,
        touch_interval: timedelta = TOUCH_INTERVAL,
    ) -> None:
        self._engine = engine
        self._lifetimes = lifetimes
        self._box = tokenbox
        self._clock = clock
        self._touch_interval = touch_interval

    def __repr__(self) -> str:
        return "SessionStore()"

    # --- the request path ---------------------------------------------------------------

    def lookup(self, token: str) -> Session | None:
        """The live session for a cookie value, or None. One query; fails closed."""
        if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
            return None
        now = self._clock()
        with self._engine.connect() as connection:
            row = connection.execute(
                _joined_select().where(sessions.c.token_hash == hash_token(token))
            ).first()
        if row is None:
            return None
        role = _live_role(row, now)
        if role is None:
            return None
        session = _to_session(row, role)
        if now - row.last_seen_at >= self._touch_interval:
            return self._touch(session, now)
        return session

    def _touch(self, session: Session, now: datetime) -> Session | None:
        """Slide the idle time. A session that vanished meanwhile is no session."""
        idle = min(now + self._lifetimes(session.role.value).idle, session.absolute_expires_at)
        with UnitOfWork(self._engine) as db:
            result = db.execute(
                update(sessions)
                .where(
                    sessions.c.id == session.id,
                    sessions.c.revoked_at.is_(None),
                    sessions.c.last_seen_at <= now - self._touch_interval,
                )
                .values(last_seen_at=now, idle_expires_at=idle)
            )
        if _rows(result) == 0:
            # Revoked, or touched by a parallel request: look again rather than guess.
            return self._reload(session.id, now)
        return replace(session, last_seen_at=now, idle_expires_at=idle)

    def _reload(self, session_id: UUID, now: datetime) -> Session | None:
        with self._engine.connect() as connection:
            row = connection.execute(_joined_select().where(sessions.c.id == session_id)).first()
        if row is None:
            return None
        role = _live_role(row, now)
        return None if role is None else _to_session(row, role)

    # --- creating, rotating, ending -----------------------------------------------------

    def create(
        self,
        user_id: UUID,
        *,
        auth_time: datetime,
        client_ip: str | None = None,
        user_agent: str | None = None,
        refresh_token: SecretStr | None = None,
        replaces: str | None = None,
    ) -> IssuedSession:
        """Start a session for an active person, with the lifetimes of their role.

        The absolute time never passes the person's own `expires_at`. `replaces` is the cookie
        value the browser presented, if any: that session ends ("replaced"), so a login never
        keeps an older session of the browser alive. `SessionRefused` for a person who is
        unknown, disabled or expired.
        """
        now = self._clock()
        with UnitOfWork(self._engine) as db:
            person = db.execute(
                select(
                    users.c.role,
                    users.c.status,
                    users.c.expires_at,
                    users.c.username,
                    users.c.display_name,
                ).where(users.c.id == user_id)
            ).first()
            if person is None or person.status != "active":
                raise SessionRefused
            if person.expires_at is not None and person.expires_at <= now:
                raise SessionRefused
            try:
                role = Role(person.role)
            except ValueError:
                raise SessionRefused from None
            life = self._lifetimes(role.value)
            absolute = now + life.absolute
            if person.expires_at is not None:
                absolute = min(absolute, person.expires_at)
            idle = min(now + life.idle, absolute)
            row_id, token, csrf = uuid7(), new_token(), new_token()
            sealed = (
                self._box.seal(
                    Purpose.REFRESH_TOKEN, row_id, refresh_token.get_secret_value().encode()
                )
                if refresh_token is not None
                else None
            )
            replaced = Revocation(0)
            if replaces is not None and TOKEN_RE.fullmatch(replaces):
                replaced = _revoke(
                    db, sessions.c.token_hash == hash_token(replaces), "replaced", now, self._box
                )
            db.execute(
                sessions.insert().values(
                    id=row_id,
                    token_hash=hash_token(token),
                    user_id=user_id,
                    auth_time=auth_time,
                    created_at=now,
                    last_seen_at=now,
                    idle_expires_at=idle,
                    absolute_expires_at=absolute,
                    user_agent=_clean_user_agent(user_agent),
                    client_ip=_clean_address(client_ip),
                    csrf_token=csrf,
                    idp_tokens_enc=sealed,
                    revalidate_after=now,
                )
            )
        session = Session(
            id=row_id,
            user_id=user_id,
            username=person.username,
            display_name=person.display_name,
            role=role,
            auth_time=auth_time,
            created_at=now,
            last_seen_at=now,
            idle_expires_at=idle,
            absolute_expires_at=absolute,
            csrf_token=csrf,
        )
        return IssuedSession(session=session, token=SessionToken(token), replaced=replaced)

    def rotate(
        self, session_id: UUID, *, auth_time: datetime | None = None
    ) -> IssuedSession | None:
        """A new cookie value and CSRF token for a live session (login, step-up, role change).

        The row stays; its stored hash changes, so the old cookie stops working at once.
        `auth_time` moves forward on a step-up. None if the session is no longer live.
        """
        now = self._clock()
        token, csrf = new_token(), new_token()
        values: dict[str, Any] = {"token_hash": hash_token(token), "csrf_token": csrf}
        if auth_time is not None:
            values["auth_time"] = auth_time
        with UnitOfWork(self._engine) as db:
            updated = db.execute(
                update(sessions)
                .where(sessions.c.id == session_id, sessions.c.revoked_at.is_(None))
                .values(**values)
                .returning(sessions.c.id)
            ).first()
            if updated is None:
                return None
            row = db.execute(_joined_select().where(sessions.c.id == session_id)).first()
        assert row is not None
        role = _live_role(row, now)
        if role is None:
            return None
        return IssuedSession(session=_to_session(row, role), token=SessionToken(token))

    def revoke(self, session_id: UUID, reason: str) -> Revocation:
        """End one session and clear its IdP token. `ended` is 0 if it was already over."""
        _check_reason(reason)
        with UnitOfWork(self._engine) as db:
            return _revoke(db, sessions.c.id == session_id, reason, self._clock(), self._box)

    def revoke_token(self, token: str, reason: str) -> Revocation:
        """End the session a cookie value belongs to (logout). `ended` is 0 if there is none."""
        _check_reason(reason)
        if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
            return Revocation(0)
        where = sessions.c.token_hash == hash_token(token)
        with UnitOfWork(self._engine) as db:
            return _revoke(db, where, reason, self._clock(), self._box)

    def end_user_sessions(self, user_id: UUID, reason: str) -> Revocation:
        """End all of a person's sessions in their own transaction. See the module function."""
        with UnitOfWork(self._engine) as db:
            revocation = end_user_sessions(db, user_id, reason, box=self._box, now=self._clock())
        if revocation.ended:
            logger.info("ended %d session(s) of user %s (%s)", revocation.ended, user_id, reason)
        return revocation

    def take_refresh_token(self, session_id: UUID) -> SecretStr | None:
        """The stored IdP refresh token of a session, removed from the row as it is read.

        For revoking it at the IdP (logout, disable): whoever takes it is the one to revoke
        it, and it is gone from the database either way. None if there is none, or if it
        cannot be read (a `SECRET_KEY` that no longer opens it).
        """
        with UnitOfWork(self._engine) as db:
            blob = db.execute(
                select(sessions.c.idp_tokens_enc)
                .where(sessions.c.id == session_id)
                .with_for_update()
            ).scalar_one_or_none()
            if blob is None:
                return None
            db.execute(
                update(sessions).where(sessions.c.id == session_id).values(idp_tokens_enc=None)
            )
        try:
            return SecretStr(
                self._box.open(Purpose.REFRESH_TOKEN, session_id, bytes(blob)).decode()
            )
        except TokenBoxError:
            logger.warning("a stored IdP token could not be read for user session %s", session_id)
            return None

    def purge(self, retention: timedelta = DEFAULT_RETENTION) -> int:
        """Delete sessions that ended or expired more than `retention` ago, and old logins."""
        now = self._clock()
        cutoff = now - retention
        with UnitOfWork(self._engine) as db:
            result = db.execute(
                delete(sessions).where(
                    or_(sessions.c.absolute_expires_at < cutoff, sessions.c.revoked_at < cutoff)
                )
            )
            db.execute(delete(login_attempts).where(login_attempts.c.expires_at < now))
        return _rows(result)
