"""An in-memory `IdentityAdmin` for the phases that build on identity (5c, 8, ...).

It follows the same rules as the `kanidm` adapter and shows the same behaviour, so a test
that passes against it says something about the real thing. The contract suite
(tests/kernel/identity/test_admin_contract.py) runs against both.

What it models:

- **The role-group invariant:** a person is in exactly one role group. A person that is
  not (`break_roles`) is refused by every method but `set_role(..., repair=True)` and
  `delete`.
- **`set_role` in two steps**, new group first and old group after, with one undo and
  `PartialFailure("two_roles")` when the undo fails; `create_person`'s undo and
  `PartialFailure("no_role")`.
- **Failure points** (`fail_at`): the places where Kanidm can fail between two writes.
- **Reset links** (ADR-001, probes P2/P8): a link has the IdP's public host, a short
  lifetime, cannot be revoked, and a new one does not cancel an older one; committing a
  credential consumes only the link used. `open_reset_link` and `commit_credential` are
  the person's side, for tests.
- **Sessions** (probe P9): `login` gives a session. Disabling refuses it, enabling makes
  it valid again until its lifetime ends, and a credential change ends it for good.
  Role groups have the IdP's lifetimes: 28 800 s for owner and admin, 86 400 s otherwise.
- **Names:** the rules of `validate_username` and `validate_display_name`, and a clash
  with the name of a group or account (409 at Kanidm).

It holds no secret worth protecting, but it keeps the same discipline: a `ResetLink`
shows nothing of its token.
"""

from __future__ import annotations

import asyncio
import secrets
import uuid as uuidlib
import weakref
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from urllib.parse import parse_qs, quote, urlsplit

from atlas.kernel.identity.admin import (
    APP_GROUP_NAMES,
    RESET_TTL_DEFAULT,
    ROLE_GROUP_NAMES,
    App,
    IdentityConfigError,
    IdentityUnavailable,
    PartialFailure,
    Person,
    PersonDisabled,
    PersonExists,
    PersonNotFound,
    PersonRef,
    Readiness,
    ResetLink,
    Role,
    RoleInvariantError,
    check_reset_ttl,
    validate_display_name,
    validate_username,
)

# Names Kanidm already holds (groups and accounts): a person cannot take them (409).
TAKEN_NAMES = frozenset(
    {"atlas_users", "idm_all_persons", *ROLE_GROUP_NAMES.values(), *APP_GROUP_NAMES.values()}
)
SESSION_SECONDS = {
    Role.OWNER: 28_800,
    Role.ADMIN: 28_800,
    Role.ADULT: 86_400,
    Role.CHILD: 86_400,
    Role.GUEST: 86_400,
}


class FailurePoint(StrEnum):
    """A place where the identity provider can fail between two of its writes."""

    UNAVAILABLE = "unavailable"  # the next call of any method
    CREATE_ADD_ROLE = "create.add_role"  # the person exists, the role cannot be added
    CREATE_ROLLBACK = "create.rollback"  # ... and the person cannot be removed again
    SET_ROLE_ADD = "set_role.add"  # adding the new role group
    SET_ROLE_REMOVE = "set_role.remove"  # removing the old one, after the new was added
    SET_ROLE_ROLLBACK = "set_role.rollback"  # undoing after a failed removal
    APP_WRITE = "app.write"
    DELETE = "delete"


class FakeClock:
    """A clock the test moves."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 11, 17, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self._now

    def advance(self, by: timedelta) -> None:
        self._now += by


@dataclass
class _Record:
    ref: PersonRef
    username: str
    display_name: str
    roles: set[Role]
    apps: set[App] = field(default_factory=set)
    expire: datetime | None = None
    credential: int = 0  # bumped by every committed credential; 0 = none yet


@dataclass(frozen=True)
class FakeSession:
    """A person's session at the IdP, as `login` returned it."""

    person: PersonRef
    credential: int
    issued_at: datetime
    lifetime: timedelta


@dataclass(frozen=True)
class ResetSession:
    """What opening a reset link gives the person: a way to commit a credential."""

    person: PersonRef
    token: str = field(repr=False)


class FakeIdentityAdmin:
    def __init__(
        self,
        *,
        public_url: str = "https://id.test.invalid",
        clock: FakeClock | None = None,
        apps: Iterable[App] = (App.IMMICH,),
    ) -> None:
        parts = urlsplit(public_url)
        self._origin = f"https://{parts.netloc}"
        self.clock = clock or FakeClock()
        self._apps = frozenset(apps)
        self._people: dict[str, _Record] = {}
        self._intents: dict[str, tuple[PersonRef, datetime]] = {}
        self._failures: dict[FailurePoint, int] = {}
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        self.strays: list[str] = []  # members of the six groups that are not persons

    # --- test hooks ------------------------------------------------------------------

    def fail_at(self, point: FailurePoint, *, times: int = 1) -> None:
        """Make the next `times` arrivals at `point` fail with `IdentityUnavailable`."""
        self._failures[FailurePoint(point)] = times

    def _arrive(self, point: FailurePoint) -> None:
        for p in (FailurePoint.UNAVAILABLE, point):
            left = self._failures.get(p, 0)
            if left > 0:
                self._failures[p] = left - 1
                raise IdentityUnavailable(f"the identity provider failed (injected at {p.value})")

    def break_roles(self, ref: PersonRef, roles: Iterable[Role]) -> None:
        """Put a person in exactly these role groups, as an IdP admin could outside Atlas."""
        self._people[ref.uuid].roles = set(roles)

    def roles_of(self, ref: PersonRef) -> frozenset[Role]:
        return frozenset(self._people[ref.uuid].roles)

    def live_intents(self, ref: PersonRef) -> int:
        now = self.clock()
        return sum(1 for p, exp in self._intents.values() if p == ref and exp > now)

    # --- the person's side of a reset link --------------------------------------------

    def open_reset_link(self, link: ResetLink) -> ResetSession:
        """What the browser does: exchange the token. It does not consume it."""
        token = parse_qs(urlsplit(link.reveal()).query).get("token", [""])[0]
        entry = self._intents.get(token)
        if entry is None or entry[1] <= self.clock() or entry[0].uuid not in self._people:
            raise ValueError("the reset link is not valid")
        return ResetSession(entry[0], token)

    def commit_credential(self, session: ResetSession) -> None:
        """The person sets a credential. Consumes this token only; ends old sessions."""
        entry = self._intents.get(session.token)
        if entry is None or entry[1] <= self.clock():
            raise ValueError("the reset link is not valid")
        del self._intents[session.token]
        self._people[session.person.uuid].credential += 1

    def login(self, ref: PersonRef) -> FakeSession:
        record = self._people[ref.uuid]
        if not record.credential or not self._enabled(record):
            raise ValueError("the person cannot sign in")
        role = min(record.roles, key=lambda r: SESSION_SECONDS[r])
        return FakeSession(
            ref, record.credential, self.clock(), timedelta(seconds=SESSION_SECONDS[role])
        )

    def session_alive(self, session: FakeSession) -> bool:
        record = self._people.get(session.person.uuid)
        return (
            record is not None
            and self._enabled(record)
            and session.credential == record.credential
            and self.clock() < session.issued_at + session.lifetime
        )

    # --- internals ----------------------------------------------------------------------

    def _enabled(self, record: _Record) -> bool:
        return record.expire is None or record.expire > self.clock()

    def _lock(self, ref: PersonRef) -> asyncio.Lock:
        lock = self._locks.get(ref.uuid)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[ref.uuid] = lock
        return lock

    def _person(self, record: _Record) -> Person:
        (role,) = record.roles
        return Person(
            ref=record.ref,
            username=record.username,
            display_name=record.display_name,
            role=role,
            apps=frozenset(record.apps),
            enabled=self._enabled(record),
            account_expire=record.expire,
        )

    def _find(self, ref: PersonRef, *, strict: bool = True) -> _Record | None:
        record = self._people.get(ref.uuid)
        if record is not None and strict and len(record.roles) != 1:
            raise RoleInvariantError(ref, record.roles)
        return record

    def _need(self, ref: PersonRef) -> _Record:
        record = self._find(ref)
        if record is None:
            raise PersonNotFound(ref)
        return record

    # --- IdentityAdmin -------------------------------------------------------------------

    async def create_person(
        self, username: str, display_name: str, role: Role, *, apps: Iterable[App] = ()
    ) -> Person:
        self._arrive(FailurePoint.UNAVAILABLE)
        username = validate_username(username)
        display_name = validate_display_name(display_name)
        role = Role(role)
        wanted = {App(a) for a in apps}
        if not wanted <= self._apps:
            raise IdentityConfigError("an app is not set up in the identity provider")
        if username in TAKEN_NAMES or any(p.username == username for p in self._people.values()):
            raise PersonExists()
        ref = PersonRef(str(uuidlib.uuid4()))
        record = _Record(ref, username, display_name, set())
        self._people[ref.uuid] = record
        async with self._lock(ref):
            try:
                self._arrive(FailurePoint.CREATE_ADD_ROLE)
                record.roles.add(role)
                record.apps |= wanted
            except IdentityUnavailable as exc:
                try:
                    self._arrive(FailurePoint.CREATE_ROLLBACK)
                except IdentityUnavailable:
                    raise PartialFailure(ref, "no_role", "create_person") from None
                del self._people[ref.uuid]
                raise exc from None
        return self._person(record)

    async def get_person(self, ref: PersonRef) -> Person | None:
        self._arrive(FailurePoint.UNAVAILABLE)
        record = self._find(ref)
        return None if record is None else self._person(record)

    async def set_role(self, ref: PersonRef, role: Role, *, repair: bool = False) -> Person:
        self._arrive(FailurePoint.UNAVAILABLE)
        role = Role(role)
        async with self._lock(ref):
            record = self._find(ref, strict=not repair)
            if record is None:
                raise PersonNotFound(ref)
            if record.roles == {role}:
                return self._person(record)
            before = set(record.roles)
            others = sorted(before - {role})
            added = role not in before
            removed: list[Role] = []
            try:
                if added:
                    self._arrive(FailurePoint.SET_ROLE_ADD)
                    record.roles.add(role)
                    await asyncio.sleep(0)  # a real write yields: a second task could run here
                for old in others:
                    await asyncio.sleep(0)
                    self._arrive(FailurePoint.SET_ROLE_REMOVE)
                    record.roles.discard(old)
                    removed.append(old)
            except IdentityUnavailable as exc:
                try:
                    self._arrive(FailurePoint.SET_ROLE_ROLLBACK)
                except IdentityUnavailable:
                    raise PartialFailure(ref, "two_roles", "set_role") from None
                if added:
                    record.roles.discard(role)
                record.roles |= set(removed)
                raise exc from None
            return self._person(record)

    async def set_app_access(self, ref: PersonRef, app: App, allowed: bool) -> Person:
        self._arrive(FailurePoint.UNAVAILABLE)
        app = App(app)
        if app not in self._apps:
            raise IdentityConfigError(f"{app.value} is not set up in the identity provider")
        async with self._lock(ref):
            record = self._need(ref)
            self._arrive(FailurePoint.APP_WRITE)
            if allowed:
                record.apps.add(app)
            else:
                record.apps.discard(app)
            return self._person(record)

    async def issue_credential_reset(
        self, ref: PersonRef, *, ttl: timedelta = RESET_TTL_DEFAULT
    ) -> ResetLink:
        self._arrive(FailurePoint.UNAVAILABLE)
        seconds = check_reset_ttl(ttl)
        record = self._need(ref)
        if not self._enabled(record):
            raise PersonDisabled(ref)
        token = secrets.token_urlsafe(17)
        expires = self.clock() + timedelta(seconds=seconds)
        self._intents[token] = (ref, expires)
        return ResetLink(f"{self._origin}/ui/reset?token={quote(token, safe='')}", expires)

    async def disable(self, ref: PersonRef) -> Person:
        self._arrive(FailurePoint.UNAVAILABLE)
        record = self._need(ref)
        if self._enabled(record):
            record.expire = self.clock()
        return self._person(record)

    async def enable(self, ref: PersonRef) -> Person:
        self._arrive(FailurePoint.UNAVAILABLE)
        record = self._need(ref)
        if record.expire is not None and record.expire <= self.clock():
            record.expire = None
        return self._person(record)

    async def delete(self, ref: PersonRef) -> bool:
        self._arrive(FailurePoint.UNAVAILABLE)
        async with self._lock(ref):
            if ref.uuid not in self._people:
                return False
            self._arrive(FailurePoint.DELETE)
            del self._people[ref.uuid]
            for token in [t for t, (p, _) in self._intents.items() if p == ref]:
                del self._intents[token]
            return True

    async def check(self) -> Readiness:
        return Readiness(
            tuple(f"a role group has a member that is not a person: {name}" for name in self.strays)
        )


__all__ = [
    "FailurePoint",
    "FakeClock",
    "FakeIdentityAdmin",
    "FakeSession",
    "ResetSession",
]
