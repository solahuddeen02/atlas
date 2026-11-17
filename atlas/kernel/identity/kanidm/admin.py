"""The `kanidm` IdentityAdmin adapter: the bundled IdP (ADR-001 "Provisioning").

It acts with the console's API token (`ATLAS_IDP_ADMIN_TOKEN`), which since Phase 5b-3a
can manage persons and exactly six groups: the five role groups and `immich_users`.
Never with `idm_admin`.

Checked against Kanidm 1.12.0 (Phase 5b-3 probes), and what the code relies on:

- A person's `directmemberof` holds group SPNs (`name@domain`), not UUIDs. The role
  groups are found by UUID (`groups.json`), resolved to their *current* SPN
  (`GET /v1/group/{uuid}`, cached for 30 s), and the person's groups are matched against
  those SPNs. A group that only has the same name, with another UUID, never counts.
- Adding a member is idempotent, and so is removing one that is not a member. A member
  that does not exist is a 500. There is no call that swaps groups: `set_role` adds the
  new group first and removes the old one after, so a person is never in zero groups and
  the stricter of two credential policies applies in between.
- A person's UUID read from the person endpoint is `200 null` when it is the UUID of a
  service account; the client also refuses an entry without the class `person`.
- `account_expire` in the past disables: existing sessions are refused and a login is
  denied. Clearing it makes the old sessions valid again until their own lifetime.
- A reset token cannot be revoked and a new one does not cancel an older one.

Role changes of one person are serialised by an in-process lock. atlas-api runs one
process on purpose (atlas/kernel/web/__main__.py): two processes racing left a person in
two role groups in every round of the probe.

Logs hold the operation and a person's UUID, never a name, a token or a link.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import weakref
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, Self
from urllib.parse import quote, urlsplit

from atlas.kernel.identity.admin import (
    _UUID,
    APP_GROUP_NAMES,
    ROLE_GROUP_NAMES,
    App,
    IdentityConfigError,
    IdentityError,
    IdentityPermissionDenied,
    IdentityRejected,
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
from atlas.kernel.identity.admin import RESET_TTL_DEFAULT as _DEFAULT_TTL
from atlas.kernel.identity.kanidm.client import Entry, KanidmClient, ResetIntent
from atlas.kernel.identity.kanidm.errors import (
    KanidmAuthError,
    KanidmConflict,
    KanidmError,
    KanidmInvalidRequest,
    KanidmPermissionDenied,
)

if TYPE_CHECKING:
    from atlas.kernel.config import Settings

logger = logging.getLogger("atlas.identity.admin")

SPN_CACHE_SECONDS = 30.0


class AdminApi(Protocol):
    """The calls of `KanidmClient` that this adapter makes."""

    async def get_person(self, ident: str) -> Entry | None: ...
    async def create_person(self, name: str, *, display_name: str) -> None: ...
    async def delete_person(self, ident: str) -> bool: ...
    async def set_person_account_expire(self, ident: str, when: datetime) -> None: ...
    async def clear_person_account_expire(self, ident: str) -> None: ...
    async def issue_credential_reset_intent(
        self, ident: str, *, ttl_seconds: int
    ) -> ResetIntent: ...
    async def get_group(self, name: str) -> Entry | None: ...
    async def add_group_members(self, group: str, members: Sequence[str]) -> None: ...
    async def remove_group_members(self, group: str, members: Sequence[str]) -> None: ...
    async def get_service_account(self, name: str) -> Entry | None: ...


@dataclass(frozen=True)
class RoleGroups:
    """The UUIDs of the role groups and the app groups, from `groups.json`."""

    roles: Mapping[Role, str]
    apps: Mapping[App, str]

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> RoleGroups:
        """`{group name: UUID}` as the IdP bootstrap writes it. The five role groups are
        required; an app group is optional (no Immich, no `immich_users`)."""

        def uuid_of(name: str) -> str | None:
            value = data.get(name)
            if value is None:
                return None
            if not isinstance(value, str) or not _UUID.fullmatch(value):
                raise IdentityConfigError(f"groups.json: the entry for {name} is not a UUID")
            return value

        roles: dict[Role, str] = {}
        for role, name in ROLE_GROUP_NAMES.items():
            value = uuid_of(name)
            if value is None:
                raise IdentityConfigError(
                    f"groups.json has no entry for {name}; re-run the IdP bootstrap"
                )
            roles[role] = value
        apps = {app: v for app, name in APP_GROUP_NAMES.items() if (v := uuid_of(name))}
        if len(set(roles.values())) != len(roles) or set(roles.values()) & set(apps.values()):
            raise IdentityConfigError("groups.json lists one UUID for two groups")
        return cls(roles=roles, apps=apps)

    @classmethod
    def load(cls, path: Path) -> RoleGroups:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise IdentityConfigError(
                "groups.json is missing or unreadable; re-run the IdP bootstrap"
            ) from None
        if not isinstance(data, dict):
            raise IdentityConfigError("groups.json is not a mapping; re-run the IdP bootstrap")
        return cls.from_mapping(data)

    def label(self, uuid: str) -> str:
        for role, value in self.roles.items():
            if value == uuid:
                return ROLE_GROUP_NAMES[role]
        for app, value in self.apps.items():
            if value == uuid:
                return APP_GROUP_NAMES[app]
        return "group"

    @property
    def managed(self) -> dict[str, str]:
        """Group name -> UUID of the groups the console manages."""
        return {
            **{ROLE_GROUP_NAMES[r]: u for r, u in self.roles.items()},
            **{APP_GROUP_NAMES[a]: u for a, u in self.apps.items()},
        }


def _translate(exc: KanidmError) -> IdentityError:
    """An IdentityError for a Kanidm error. The text is fixed; Kanidm's own is not used."""
    if isinstance(exc, KanidmAuthError | KanidmPermissionDenied):
        return IdentityPermissionDenied("the identity provider refused the console's token")
    if isinstance(exc, KanidmConflict):
        return PersonExists()
    if isinstance(exc, KanidmInvalidRequest):
        return IdentityRejected("the identity provider refused the request")
    status = f" (HTTP {exc.status})" if exc.status is not None else ""
    return IdentityUnavailable(f"the identity provider failed: {exc.operation}{status}")


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class _Found:
    entry: Entry
    ref: PersonRef
    username: str
    roles: list[Role]
    apps: set[App]


class KanidmIdentityAdmin:
    def __init__(
        self,
        api: AdminApi,
        groups: RoleGroups,
        *,
        public_url: str,
        now: Callable[[], datetime] = _now,
        monotonic: Callable[[], float] = time.monotonic,
        close: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        parts = urlsplit(public_url)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.path not in ("", "/")
            or parts.query
            or parts.fragment
        ):
            raise ValueError("public_url must be the IdP's https origin")
        self._origin = f"https://{parts.netloc}"
        self._api = api
        self._groups = groups
        self._now = now
        self._monotonic = monotonic
        self._close = close
        self._spns: dict[str, tuple[str, float]] = {}
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

    @classmethod
    def from_settings(cls, settings: Settings) -> KanidmIdentityAdmin:
        """The adapter for the bundled IdP, from `Settings`.

        It uses the console's token, the internal URL and CA (every call is verified
        against that CA) and groups.json. The public URL is only for the links people
        open. Close it with `aclose()`.
        """
        if not settings.bundled_idp or settings.idp_admin_token is None:
            raise IdentityConfigError("the kanidm adapter needs the bundled IdP and its token")
        assert settings.idp_public_url is not None
        try:
            client = KanidmClient(
                base_url=settings.idp_internal_url,
                ca_file=settings.idp_ca_file,
                token=settings.idp_admin_token,
            )
        except (OSError, ValueError):
            raise IdentityConfigError(
                "the IdP's CA certificate cannot be read (ATLAS_IDP_CA_FILE)"
            ) from None
        return cls(
            client,
            RoleGroups.load(settings.idp_groups_file),
            public_url=settings.idp_public_url,
            close=client.aclose,
        )

    async def aclose(self) -> None:
        if self._close is not None:
            await self._close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # --- reading -----------------------------------------------------------------

    def _lock(self, ref: PersonRef) -> asyncio.Lock:
        lock = self._locks.get(ref.uuid)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[ref.uuid] = lock
        return lock

    async def _spn(self, uuid: str) -> str:
        """The current SPN of a group, found by its UUID."""
        cached = self._spns.get(uuid)
        if cached is not None and cached[1] > self._monotonic():
            return cached[0]
        entry = await self._api.get_group(uuid)
        spn = entry.first("spn") if entry is not None else None
        if entry is None or entry.uuid != uuid or not spn:
            raise IdentityConfigError(
                f"the group {self._groups.label(uuid)} ({uuid}) does not exist in the identity "
                "provider; re-run the IdP bootstrap"
            )
        self._spns[uuid] = (spn, self._monotonic() + SPN_CACHE_SECONDS)
        return spn

    async def _read(self, ref: PersonRef) -> _Found | None:
        entry = await self._api.get_person(ref.uuid)
        if entry is None or entry.uuid != ref.uuid:
            return None
        memberof = set(entry.attrs.get("directmemberof", ()))
        roles = [r for r, u in self._groups.roles.items() if await self._spn(u) in memberof]
        apps = {a for a, u in self._groups.apps.items() if await self._spn(u) in memberof}
        return _Found(entry, ref, entry.name or "", sorted(roles), apps)

    async def _load(self, ref: PersonRef, *, strict: bool = True) -> _Found | None:
        found = await self._read(ref)
        if found is not None and strict and len(found.roles) != 1:
            # A renamed group would read as "no role": look the SPNs up again once.
            self._spns.clear()
            found = await self._read(ref)
        if found is not None and strict and len(found.roles) != 1:
            raise RoleInvariantError(ref, found.roles)
        return found

    def _expire(self, entry: Entry) -> datetime | None:
        raw = entry.first("account_expire")
        if raw is None:
            return None
        try:
            when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            when = None
        if when is None or when.tzinfo is None:
            raise IdentityUnavailable(
                "the identity provider sent an account expiry in an unknown format"
            )
        return when

    def _person(self, found: _Found) -> Person:
        expire = self._expire(found.entry)
        role = found.roles[0]
        return Person(
            ref=found.ref,
            username=found.username,
            display_name=found.entry.first("displayname") or "",
            role=role,
            apps=frozenset(found.apps),
            enabled=expire is None or expire > self._now(),
            account_expire=expire,
        )

    async def get_person(self, ref: PersonRef) -> Person | None:
        try:
            found = await self._load(ref)
        except KanidmError as exc:
            raise _translate(exc) from exc
        return None if found is None else self._person(found)

    # --- creating and deleting ----------------------------------------------------

    async def create_person(
        self,
        username: str,
        display_name: str,
        role: Role,
        *,
        apps: Iterable[App] = (),
    ) -> Person:
        username = validate_username(username)
        display_name = validate_display_name(display_name)
        role = Role(role)
        wanted = {App(a) for a in apps}
        for app in wanted:
            if app not in self._groups.apps:
                raise IdentityConfigError(f"{app.value} is not set up in the identity provider")
        try:
            await self._api.create_person(username, display_name=display_name)
            entry = await self._api.get_person(username)
        except KanidmError as exc:
            raise _translate(exc) from exc
        if entry is None or not entry.uuid:
            raise IdentityUnavailable(
                "the identity provider created the person but cannot read them"
            )
        ref = PersonRef(entry.uuid)
        role_added = False
        async with self._lock(ref):
            try:
                await self._add(self._groups.roles[role], username)
                role_added = True
                for app in sorted(wanted):
                    await self._add(self._groups.apps[app], username)
                found = await self._load(ref)
                if found is None or found.roles != [role] or found.apps != wanted:
                    raise IdentityUnavailable("the new person could not be read back as asked")
            except (KanidmError, IdentityError) as exc:
                original = _translate(exc) if isinstance(exc, KanidmError) else exc
                try:
                    removed = await self._api.delete_person(ref.uuid)
                except KanidmError:
                    removed = False
                logger.warning("create_person failed person=%s removed=%s", ref, removed)
                if removed:
                    raise original from None
                raise PartialFailure(
                    ref, "unverified" if role_added else "no_role", "create_person"
                ) from None
        logger.info("create_person person=%s role=%s", ref, role.value)
        return self._person(found)

    async def delete(self, ref: PersonRef) -> bool:
        async with self._lock(ref):
            try:
                found = await self._load(ref, strict=False)
                if found is None:
                    return False
                deleted = await self._api.delete_person(ref.uuid)
            except KanidmError as exc:
                raise _translate(exc) from exc
        logger.info("delete person=%s deleted=%s", ref, deleted)
        return deleted

    # --- groups ----------------------------------------------------------------------

    async def _add(self, group_uuid: str, username: str) -> None:
        await self._api.add_group_members(group_uuid, [username])

    async def _remove(self, group_uuid: str, username: str) -> None:
        await self._api.remove_group_members(group_uuid, [username])

    async def set_role(self, ref: PersonRef, role: Role, *, repair: bool = False) -> Person:
        role = Role(role)
        async with self._lock(ref):
            try:
                found = await self._load(ref, strict=not repair)
                if found is None:
                    raise PersonNotFound(ref)
                if found.roles == [role]:
                    return self._person(found)
                new = self._groups.roles[role]
                others = [r for r in found.roles if r != role]
                removed: list[Role] = []
                added = role not in found.roles
                try:
                    if added:
                        # First the new group: never zero groups, and the stricter policy
                        # of the two applies until the old one is gone.
                        await self._add(new, found.username)
                    for old in others:
                        await self._remove(self._groups.roles[old], found.username)
                        removed.append(old)
                except (KanidmError, asyncio.CancelledError) as exc:
                    await self._undo(ref, found, new=new if added else None, removed=removed)
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    raise _translate(exc) from exc
                after = await self._load(ref)
            except KanidmError as exc:
                raise _translate(exc) from exc
        if after is None or after.roles != [role]:
            logger.error("set_role person=%s could not be read back as one role", ref)
            raise PartialFailure(ref, "unverified", "set_role")
        logger.info("set_role person=%s role=%s", ref, role.value)
        return self._person(after)

    async def _undo(
        self, ref: PersonRef, found: _Found, *, new: str | None, removed: list[Role]
    ) -> None:
        """Put the groups back as they were, once. `PartialFailure` if that fails too.

        `new` is the UUID of the group this change added (None if the person had it);
        `removed` are the old groups already taken off. Shielded: a cancellation of the
        change must not leave the undo half done.
        """

        async def restore() -> None:
            if new is not None:
                await self._remove(new, found.username)
            for old in removed:
                await self._add(self._groups.roles[old], found.username)

        try:
            await asyncio.shield(restore())
        except KanidmError:
            logger.error("set_role person=%s could not be undone", ref)
            raise PartialFailure(ref, "two_roles", "set_role") from None

    async def set_app_access(self, ref: PersonRef, app: App, allowed: bool) -> Person:
        app = App(app)
        group = self._groups.apps.get(app)
        if group is None:
            raise IdentityConfigError(f"{app.value} is not set up in the identity provider")
        async with self._lock(ref):
            try:
                found = await self._load(ref)
                if found is None:
                    raise PersonNotFound(ref)
                if (app in found.apps) != allowed:
                    if allowed:
                        await self._add(group, found.username)
                    else:
                        await self._remove(group, found.username)
                    found = await self._load(ref)
                    if found is None:
                        raise PersonNotFound(ref)
            except KanidmError as exc:
                raise _translate(exc) from exc
        if (app in found.apps) != allowed:
            raise IdentityUnavailable(
                "the change to the person's app access could not be read back"
            )
        logger.info("set_app_access person=%s app=%s allowed=%s", ref, app.value, allowed)
        return self._person(found)

    # --- disable and enable ----------------------------------------------------------

    async def disable(self, ref: PersonRef) -> Person:
        try:
            found = await self._load(ref)
            if found is None:
                raise PersonNotFound(ref)
            if self._person(found).enabled:
                await self._api.set_person_account_expire(ref.uuid, self._now())
                found = await self._load(ref)
                if found is None:
                    raise PersonNotFound(ref)
        except KanidmError as exc:
            raise _translate(exc) from exc
        person = self._person(found)
        if person.enabled:
            raise IdentityUnavailable("the person could not be disabled")
        logger.info("disable person=%s", ref)
        return person

    async def enable(self, ref: PersonRef) -> Person:
        try:
            found = await self._load(ref)
            if found is None:
                raise PersonNotFound(ref)
            expire = self._expire(found.entry)
            # Only a past expiry is "disabled"; a future one is not ours to remove.
            if expire is not None and expire <= self._now():
                await self._api.clear_person_account_expire(ref.uuid)
                found = await self._load(ref)
                if found is None:
                    raise PersonNotFound(ref)
        except KanidmError as exc:
            raise _translate(exc) from exc
        person = self._person(found)
        if not person.enabled:
            raise IdentityUnavailable("the person could not be enabled")
        logger.info("enable person=%s", ref)
        return person

    # --- reset links -------------------------------------------------------------------

    async def issue_credential_reset(
        self, ref: PersonRef, *, ttl: timedelta = _DEFAULT_TTL
    ) -> ResetLink:
        seconds = check_reset_ttl(ttl)
        try:
            found = await self._load(ref)
            if found is None:
                raise PersonNotFound(ref)
            if not self._person(found).enabled:
                raise PersonDisabled(ref)
            intent = await self._api.issue_credential_reset_intent(ref.uuid, ttl_seconds=seconds)
        except KanidmError as exc:
            # No chaining: nothing of the exchange travels with the error.
            raise _translate(exc) from None
        # The IdP's public origin, never its internal URL; the token is encoded for a URL.
        url = f"{self._origin}/ui/reset?token={quote(intent.token.get_secret_value(), safe='')}"
        logger.info("reset link issued person=%s ttl_seconds=%d", ref, seconds)
        return ResetLink(url, intent.expiry)

    # --- check ------------------------------------------------------------------------

    async def check(self) -> Readiness:
        """Whether the console can do its work. Never raises, never holds a secret.

        Reads the groups the console manages: each must exist under the UUID in
        groups.json, and each member must be a person (a service account or a group in
        a role group is reported, by name and UUID, and `--verify` removes it).
        """
        problems: list[str] = []
        try:
            for name, uuid in self._groups.managed.items():
                problems += await self._check_group(name, uuid)
        except (KanidmAuthError, KanidmPermissionDenied):
            problems.append("the identity provider refuses the console's token")
        except KanidmError as exc:
            problems.append(f"the identity provider cannot be reached or failed: {exc.operation}")
        except IdentityError as exc:
            problems.append(str(exc))
        return Readiness(tuple(problems))

    async def _check_group(self, name: str, uuid: str) -> list[str]:
        entry = await self._api.get_group(uuid)
        if entry is None or entry.uuid != uuid:
            return [
                f"the group {name} ({uuid}) cannot be read by the console or does not exist; "
                "re-run the IdP bootstrap"
            ]
        problems = []
        for spn in sorted(entry.attrs.get("member", ())):
            member = spn.partition("@")[0]
            try:
                if await self._api.get_person(member) is not None:
                    continue
            except ValueError:
                continue  # a name only a person can have
            problems.append(
                f"the group {name} has a member that is not a person: {member} "
                f"({await self._uuid_of(member)})"
            )
        return problems

    async def _uuid_of(self, name: str) -> str:
        """The UUID of a group or service account called `name`, for the report."""
        for lookup in (self._api.get_group, self._api.get_service_account):
            try:
                entry = await lookup(name)
            except (KanidmError, ValueError):
                continue
            if entry is not None and entry.uuid:
                return entry.uuid
        return "UUID unknown"
