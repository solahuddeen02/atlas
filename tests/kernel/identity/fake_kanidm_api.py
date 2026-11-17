"""A stateful in-memory stand-in for the calls the `kanidm` IdentityAdmin adapter makes.

Like `fake_idp.py` (the bootstrap's), it answers the way Kanidm 1.12.0 does where the
adapter relies on it: names (SPNs) in `directmemberof` and `member`, `200 null` for a
missing object or for the UUID of something that is not a person, a 500 for a group
member that does not exist, 409 for a name any person, group or account holds, a reset
token that is not consumed by opening it, several live tokens at once, and a lifetime
clamped to 300..86400 seconds. Every call is recorded in `calls`.

`fail(method, [exc, None, exc, ...])` makes the next calls of `method` raise (None lets
one through), which is how the tests reach each point between two writes.
"""

from __future__ import annotations

import asyncio
import secrets
import uuid as uuidlib
from collections import defaultdict, deque
from collections.abc import Sequence
from datetime import datetime, timedelta

from pydantic import SecretStr

from atlas.kernel.identity.admin import APP_GROUP_NAMES, ROLE_GROUP_NAMES
from atlas.kernel.identity.fake import FakeClock
from atlas.kernel.identity.kanidm import (
    Entry,
    KanidmConflict,
    KanidmInvalidRequest,
    KanidmNotFound,
    KanidmServerError,
    ResetIntent,
)

DOMAIN = "id.test.invalid"
RESERVED = {"root"}  # refused at create (400), like Kanidm


class FakeKanidmApi:
    def __init__(self, clock: FakeClock | None = None) -> None:
        self.clock = clock or FakeClock()
        self.calls: list[str] = []
        self._fail: dict[str, deque[BaseException | None]] = defaultdict(deque)
        self.persons: dict[str, dict[str, object]] = {}
        self.groups: dict[str, dict[str, object]] = {}
        self.accounts: dict[str, str] = {}
        self.intents: dict[str, tuple[str, datetime]] = {}  # token -> (person uuid, expiry)
        for name in (*ROLE_GROUP_NAMES.values(), *APP_GROUP_NAMES.values(), "atlas_users"):
            self.add_group(name)
        self.add_group("idm_all_persons")
        self.accounts["atlas-console"] = str(uuidlib.uuid4())

    # --- setup and hooks ---------------------------------------------------------------

    def add_group(self, name: str) -> str:
        uuid = str(uuidlib.uuid4())
        self.groups[uuid] = {"name": name, "member": set()}
        return uuid

    def group_uuid(self, name: str) -> str:
        return next(u for u, g in self.groups.items() if g["name"] == name)

    def rename_group(self, name: str, new: str) -> None:
        self.groups[self.group_uuid(name)]["name"] = new

    def groups_json(self) -> dict[str, str]:
        wanted = (*ROLE_GROUP_NAMES.values(), *APP_GROUP_NAMES.values(), "atlas_users")
        return {name: self.group_uuid(name) for name in wanted}

    def fail(self, method: str, pattern: Sequence[BaseException | None]) -> None:
        self._fail[method] = deque(pattern)

    async def _enter(self, method: str, *args: object) -> None:
        await asyncio.sleep(0)  # a real call yields: two tasks interleave here
        self.calls.append(f"{method}:{args[0]}" if args else method)
        queue = self._fail.get(method)
        if queue:
            outcome = queue.popleft()
            if outcome is not None:
                raise outcome

    def raw_add_person(self, name: str, groups: Sequence[str] = ()) -> str:
        """A person made outside Atlas, in the named groups."""
        uuid = str(uuidlib.uuid4())
        self.persons[uuid] = {"name": name, "displayname": name.title(), "expire": None}
        self.groups[self.group_uuid("idm_all_persons")]["member"].add(self._spn(name))  # type: ignore[attr-defined]
        for group in groups:
            self.groups[self.group_uuid(group)]["member"].add(self._spn(name))  # type: ignore[attr-defined]
        return uuid

    def roles_of_person(self, uuid: str) -> set[str]:
        name = str(self.persons[uuid]["name"])
        return {
            str(g["name"])
            for g in self.groups.values()
            if self._spn(name) in g["member"]  # type: ignore[operator]
        } & set(ROLE_GROUP_NAMES.values())

    # --- helpers ---------------------------------------------------------------------------

    @staticmethod
    def _spn(name: str) -> str:
        return f"{name}@{DOMAIN}"

    def _person_by(self, ident: str) -> tuple[str, dict[str, object]] | None:
        for uuid, person in self.persons.items():
            if ident in (uuid, person["name"]):
                return uuid, person
        return None

    def _group_by(self, ident: str) -> tuple[str, dict[str, object]] | None:
        for uuid, group in self.groups.items():
            if ident in (uuid, group["name"]):
                return uuid, group
        return None

    # --- the calls ---------------------------------------------------------------------------

    async def get_person(self, ident: str) -> Entry | None:
        await self._enter("get_person", ident)
        found = self._person_by(ident)
        if found is None:
            return None  # also the UUID of a service account: `200 null`
        uuid, person = found
        name = str(person["name"])
        memberof = sorted(
            self._spn(str(g["name"]))
            for g in self.groups.values()
            if self._spn(name) in g["member"]  # type: ignore[operator]
        )
        attrs: dict[str, tuple[str, ...]] = {
            "class": ("account", "memberof", "object", "person"),
            "name": (name,),
            "spn": (self._spn(name),),
            "uuid": (uuid,),
            "displayname": (str(person["displayname"]),),
            "directmemberof": tuple(memberof),
        }
        expire = person["expire"]
        if isinstance(expire, datetime):
            attrs["account_expire"] = (expire.strftime("%Y-%m-%dT%H:%M:%SZ"),)
        return Entry(kind="person", attrs=attrs)

    async def create_person(self, name: str, *, display_name: str) -> None:
        await self._enter("create_person", name)
        if name in RESERVED:
            raise KanidmInvalidRequest("rejected", operation="POST /v1/person", status=400)
        taken = (
            {str(p["name"]) for p in self.persons.values()}
            | {str(g["name"]) for g in self.groups.values()}
            | set(self.accounts)
        )
        if name in taken:
            raise KanidmConflict("already exists", operation="POST /v1/person", status=409)
        self.raw_add_person(name)
        found = self._person_by(name)
        assert found is not None
        found[1]["displayname"] = display_name

    async def delete_person(self, ident: str) -> bool:
        await self._enter("delete_person", ident)
        found = self._person_by(ident)
        if found is None:
            return False
        uuid, person = found
        for group in self.groups.values():
            group["member"].discard(self._spn(str(person["name"])))  # type: ignore[attr-defined]
        del self.persons[uuid]
        for token in [t for t, (p, _) in self.intents.items() if p == uuid]:
            del self.intents[token]
        return True

    async def set_person_account_expire(self, ident: str, when: datetime) -> None:
        await self._enter("set_person_account_expire", ident)
        found = self._person_by(ident)
        if found is None:
            raise KanidmNotFound("no such object", operation="PUT", status=404)
        found[1]["expire"] = when.replace(microsecond=0)

    async def clear_person_account_expire(self, ident: str) -> None:
        await self._enter("clear_person_account_expire", ident)
        found = self._person_by(ident)
        if found is None:
            raise KanidmNotFound("no such object", operation="DELETE", status=404)
        found[1]["expire"] = None

    async def issue_credential_reset_intent(self, ident: str, *, ttl_seconds: int) -> ResetIntent:
        await self._enter("issue_credential_reset_intent", ident)
        found = self._person_by(ident)
        if found is None:
            raise KanidmNotFound("no such object", operation="GET", status=404)
        ttl = min(max(ttl_seconds, 300), 86400)  # Kanidm clamps without saying so
        token = secrets.token_urlsafe(17)
        expiry = self.clock() + timedelta(seconds=ttl)
        self.intents[token] = (found[0], expiry)
        return ResetIntent(token=SecretStr(token), expiry=expiry)

    async def get_group(self, name: str) -> Entry | None:
        await self._enter("get_group", name)
        found = self._group_by(name)
        if found is None:
            return None
        uuid, group = found
        attrs: dict[str, tuple[str, ...]] = {
            "name": (str(group["name"]),),
            "spn": (self._spn(str(group["name"])),),
            "uuid": (uuid,),
            "class": ("group", "object"),
        }
        if group["member"]:
            attrs["member"] = tuple(sorted(group["member"]))  # type: ignore[call-overload]
        return Entry(kind="group", attrs=attrs)

    async def add_group_members(self, group: str, members: Sequence[str]) -> None:
        await self._enter("add_group_members", group)
        found = self._group_by(group)
        if found is None:
            raise KanidmNotFound("no such object", operation="POST", status=404)
        for member in members:
            known = (
                self._person_by(member) is not None
                or member in self.accounts
                or self._group_by(member) is not None
            )
            if not known:  # Kanidm 1.12.0: a 500, not a 404
                raise KanidmServerError("Kanidm reported an error", operation="POST", status=500)
            found[1]["member"].add(self._spn(member))  # type: ignore[attr-defined]

    async def remove_group_members(self, group: str, members: Sequence[str]) -> None:
        await self._enter("remove_group_members", group)
        found = self._group_by(group)
        if found is None:
            raise KanidmNotFound("no such object", operation="DELETE", status=404)
        for member in members:
            found[1]["member"].discard(self._spn(member))  # type: ignore[attr-defined]

    async def get_service_account(self, name: str) -> Entry | None:
        await self._enter("get_service_account", name)
        if name not in self.accounts:
            return None
        return Entry(
            kind="service_account", attrs={"name": (name,), "uuid": (self.accounts[name],)}
        )
