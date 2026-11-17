"""The IdentityAdmin contract (PLAN Phase 5b-3b), run against the `fake` adapter and the
`kanidm` adapter over an in-memory Kanidm. The same assertions for both: that is what
lets later phases test against the fake.

Acceptance criteria covered here (PLAN Phase 5):
- the adapter refuses a person without exactly one role group;
- reset links use ATLAS_IDP_PUBLIC_URL (and are redacted);
- a failed change is undone once, and otherwise leaves a person every method refuses
  until `set_role(..., repair=True)` or `delete`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine, Iterable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, TypeVar
from urllib.parse import parse_qs, urlsplit

import pytest

from atlas.kernel.identity.admin import (
    App,
    IdentityAdmin,
    IdentityUnavailable,
    InvalidDisplayName,
    InvalidResetTtl,
    InvalidUsername,
    PartialFailure,
    Person,
    PersonDisabled,
    PersonExists,
    PersonNotFound,
    PersonRef,
    ResetLink,
    Role,
    RoleInvariantError,
)
from atlas.kernel.identity.fake import FailurePoint, FakeClock, FakeIdentityAdmin
from atlas.kernel.identity.kanidm.admin import KanidmIdentityAdmin, RoleGroups
from atlas.kernel.identity.kanidm.errors import KanidmServerError
from tests.kernel.identity.fake_kanidm_api import FakeKanidmApi

T = TypeVar("T")
PUBLIC = "https://id.test.invalid"
THAI = "สมชาย ใจดี"


def run(coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


def boom() -> KanidmServerError:
    return KanidmServerError("Kanidm reported an error", operation="POST /x", status=500)


@dataclass
class Harness:
    kind: str
    admin: IdentityAdmin
    clock: FakeClock
    fake: FakeIdentityAdmin | None = None
    api: FakeKanidmApi | None = None

    # --- reaching a state a test needs -------------------------------------------------

    def break_roles(self, ref: PersonRef, roles: Iterable[Role]) -> None:
        wanted = {f"atlas_role_{r.value}" for r in roles}
        if self.fake is not None:
            self.fake.break_roles(ref, roles)
            return
        assert self.api is not None
        for name in (f"atlas_role_{r.value}" for r in Role):
            members = self.api.groups[self.api.group_uuid(name)]["member"]
            person = str(self.api.persons[ref.uuid]["name"])
            spn = f"{person}@id.test.invalid"
            if name in wanted:
                members.add(spn)  # type: ignore[attr-defined]
            else:
                members.discard(spn)  # type: ignore[attr-defined]

    def roles(self, ref: PersonRef) -> set[Role]:
        if self.fake is not None:
            return set(self.fake.roles_of(ref))
        assert self.api is not None
        names = self.api.roles_of_person(ref.uuid)
        return {Role(n.removeprefix("atlas_role_")) for n in names}

    def fail(self, scenario: str) -> None:
        """Make the identity provider fail at a place between two writes."""
        if self.fake is not None:
            point = {
                "add_new": [(FailurePoint.SET_ROLE_ADD, 1)],
                "remove_old": [(FailurePoint.SET_ROLE_REMOVE, 1)],
                "remove_old_and_undo": [
                    (FailurePoint.SET_ROLE_REMOVE, 1),
                    (FailurePoint.SET_ROLE_ROLLBACK, 1),
                ],
                "create_role": [(FailurePoint.CREATE_ADD_ROLE, 1)],
                "create_role_and_rollback": [
                    (FailurePoint.CREATE_ADD_ROLE, 1),
                    (FailurePoint.CREATE_ROLLBACK, 1),
                ],
            }[scenario]
            for p, times in point:
                self.fake.fail_at(p, times=times)
            return
        assert self.api is not None
        if scenario in ("add_new", "create_role"):
            self.api.fail("add_group_members", [boom()])
        elif scenario == "remove_old":
            self.api.fail("remove_group_members", [boom()])
        elif scenario == "remove_old_and_undo":
            self.api.fail("remove_group_members", [boom(), boom()])
        elif scenario == "create_role_and_rollback":
            self.api.fail("add_group_members", [boom()])
            self.api.fail("delete_person", [boom()])

    def add_stray(self) -> None:
        if self.fake is not None:
            self.fake.strays.append("atlas-console")
            return
        assert self.api is not None
        group = self.api.groups[self.api.group_uuid("atlas_role_guest")]
        group["member"].add("atlas-console@id.test.invalid")  # type: ignore[attr-defined]

    # --- the person's side of a reset link ---------------------------------------------

    def link_valid(self, link: ResetLink) -> bool:
        token = parse_qs(urlsplit(link.reveal()).query)["token"][0]
        if self.fake is not None:
            try:
                self.fake.open_reset_link(link)
            except ValueError:
                return False
            return True
        assert self.api is not None
        entry = self.api.intents.get(token)
        return entry is not None and entry[1] > self.clock() and entry[0] in self.api.persons

    def commit(self, link: ResetLink) -> None:
        token = parse_qs(urlsplit(link.reveal()).query)["token"][0]
        if self.fake is not None:
            self.fake.commit_credential(self.fake.open_reset_link(link))
            return
        assert self.api is not None
        del self.api.intents[token]  # a commit consumes the token that was used, only


@pytest.fixture(params=["fake", "kanidm"])
def harness(request: pytest.FixtureRequest) -> Harness:
    clock = FakeClock()
    if request.param == "fake":
        fake = FakeIdentityAdmin(public_url=PUBLIC, clock=clock)
        return Harness("fake", fake, clock, fake=fake)
    api = FakeKanidmApi(clock)
    groups = RoleGroups.from_mapping(api.groups_json())
    admin = KanidmIdentityAdmin(api, groups, public_url=PUBLIC, now=clock)
    return Harness("kanidm", admin, clock, api=api)


def make(h: Harness, role: Role = Role.CHILD, name: str = "somchai", **kw: Any) -> Person:
    return run(h.admin.create_person(name, THAI, role, **kw))


# --- both adapters are IdentityAdmins ----------


def test_both_adapters_satisfy_the_protocol(harness: Harness) -> None:
    assert isinstance(harness.admin, IdentityAdmin)


# --- create and read ----------


def test_create_then_get_returns_the_same_person(harness: Harness) -> None:
    person = make(harness, Role.ADULT, apps=[App.IMMICH])
    assert person.username == "somchai" and person.display_name == THAI
    assert person.role is Role.ADULT and person.apps == {App.IMMICH}
    assert person.enabled is True and person.account_expire is None
    assert run(harness.admin.get_person(person.ref)) == person
    assert len(str(person.ref)) == 36  # the IdP's UUID, also the OIDC sub


def test_a_person_is_in_exactly_the_role_asked_for(harness: Harness) -> None:
    for i, role in enumerate(Role):
        person = make(harness, role, name=f"person{i}")
        assert harness.roles(person.ref) == {role}


@pytest.mark.parametrize(
    "name",
    [
        "",
        "a",
        "Somchai",
        "สมชาย",
        "9ab",
        "_ab",
        "-ab",
        "a b",
        "a@b",
        "a.b",
        "x" * 33,
        "admin",
        "root",
        "anonymous",
        "atlas-console",
        "idm_probe",
        "system",
    ],
)
def test_a_username_outside_the_rule_is_refused_and_creates_nothing(
    harness: Harness, name: str
) -> None:
    with pytest.raises(InvalidUsername):
        run(harness.admin.create_person(name, "Somchai", Role.CHILD))
    if harness.api is not None:
        assert not [c for c in harness.api.calls if c.startswith("create_person")]


@pytest.mark.parametrize("display", ["", "   ", "a\nb", "a\x00b", "x" * 256])
def test_a_display_name_outside_the_rule_is_refused(harness: Harness, display: str) -> None:
    with pytest.raises(InvalidDisplayName):
        run(harness.admin.create_person("somchai", display, Role.CHILD))


def test_a_display_name_is_normalised_and_trimmed(harness: Harness) -> None:
    person = run(harness.admin.create_person("somchai", "  Café  ", Role.CHILD))
    assert person.display_name == "Café"


def test_thai_emoji_and_html_display_names_are_kept(harness: Harness) -> None:
    for i, text in enumerate(["สมชาย ใจดี", "น้องมะลิ ๒๕๖๙", "Sam 😀", "<script>alert(1)</script>"]):
        person = run(harness.admin.create_person(f"person{i}", text, Role.CHILD))
        assert person.display_name == text


def test_a_username_that_exists_or_names_a_group_is_refused(harness: Harness) -> None:
    make(harness)
    with pytest.raises(PersonExists):
        make(harness)
    with pytest.raises(PersonExists):
        make(harness, name="atlas_role_owner")  # Kanidm: a group has that name


def test_an_app_that_is_not_set_up_is_refused() -> None:
    api = FakeKanidmApi()
    groups = api.groups_json()
    del groups["immich_users"]
    admin = KanidmIdentityAdmin(
        api, RoleGroups.from_mapping(groups), public_url=PUBLIC, now=api.clock
    )
    from atlas.kernel.identity.admin import IdentityConfigError

    with pytest.raises(IdentityConfigError):
        run(admin.create_person("somchai", "Somchai", Role.CHILD, apps=[App.IMMICH]))


def test_a_missing_person_reads_as_none(harness: Harness) -> None:
    person = make(harness)
    assert run(harness.admin.delete(person.ref)) is True
    assert run(harness.admin.get_person(person.ref)) is None
    assert run(harness.admin.delete(person.ref)) is False  # idempotent
    with pytest.raises(PersonNotFound):
        run(harness.admin.set_role(person.ref, Role.ADULT))


# --- roles ----------


def test_set_role_moves_the_person_and_is_idempotent(harness: Harness) -> None:
    person = make(harness, Role.CHILD)
    moved = run(harness.admin.set_role(person.ref, Role.ADMIN))
    assert moved.role is Role.ADMIN and harness.roles(person.ref) == {Role.ADMIN}
    assert run(harness.admin.set_role(person.ref, Role.ADMIN)).role is Role.ADMIN
    for role in Role:
        assert run(harness.admin.set_role(person.ref, role)).role is role
        assert harness.roles(person.ref) == {role}


@pytest.mark.parametrize("roles", [[], [Role.CHILD, Role.ADULT], list(Role)])
def test_every_method_refuses_a_person_without_exactly_one_role(
    harness: Harness, roles: list[Role]
) -> None:
    person = make(harness)
    harness.break_roles(person.ref, roles)
    refused: list[Callable[[], Coroutine[Any, Any, Any]]] = [
        lambda: harness.admin.get_person(person.ref),
        lambda: harness.admin.set_role(person.ref, Role.OWNER),
        lambda: harness.admin.set_app_access(person.ref, App.IMMICH, True),
        lambda: harness.admin.issue_credential_reset(person.ref),
        lambda: harness.admin.disable(person.ref),
        lambda: harness.admin.enable(person.ref),
    ]
    for call in refused:
        with pytest.raises(RoleInvariantError) as raised:
            run(call())
        assert set(raised.value.roles) == set(roles)
    assert harness.roles(person.ref) == set(roles)  # refusing changed nothing


@pytest.mark.parametrize("roles", [[], [Role.CHILD, Role.ADULT], list(Role)])
def test_repair_puts_such_a_person_right_and_delete_works_on_them(
    harness: Harness, roles: list[Role]
) -> None:
    person = make(harness)
    harness.break_roles(person.ref, roles)
    fixed = run(harness.admin.set_role(person.ref, Role.GUEST, repair=True))
    assert fixed.role is Role.GUEST and harness.roles(person.ref) == {Role.GUEST}
    harness.break_roles(person.ref, roles)
    assert run(harness.admin.delete(person.ref)) is True


def test_repair_can_keep_one_of_the_roles_a_person_already_has(harness: Harness) -> None:
    person = make(harness)
    harness.break_roles(person.ref, [Role.CHILD, Role.ADULT])
    run(harness.admin.set_role(person.ref, Role.ADULT, repair=True))
    assert harness.roles(person.ref) == {Role.ADULT}


# --- a failure between two writes ----------


def test_a_failed_add_changes_nothing(harness: Harness) -> None:
    person = make(harness, Role.CHILD)
    harness.fail("add_new")
    with pytest.raises(IdentityUnavailable):
        run(harness.admin.set_role(person.ref, Role.ADULT))
    assert harness.roles(person.ref) == {Role.CHILD}
    assert run(harness.admin.get_person(person.ref)) is not None  # still usable


def test_a_failed_removal_is_undone_once(harness: Harness) -> None:
    person = make(harness, Role.CHILD)
    harness.fail("remove_old")
    with pytest.raises(IdentityUnavailable):
        run(harness.admin.set_role(person.ref, Role.ADULT))
    assert harness.roles(person.ref) == {Role.CHILD}  # back as it was
    assert run(harness.admin.get_person(person.ref)) is not None
    assert run(harness.admin.set_role(person.ref, Role.ADULT)).role is Role.ADULT  # and works


def test_a_failed_undo_leaves_two_roles_and_every_method_refuses_until_repair(
    harness: Harness,
) -> None:
    person = make(harness, Role.CHILD)
    harness.fail("remove_old_and_undo")
    with pytest.raises(PartialFailure) as raised:
        run(harness.admin.set_role(person.ref, Role.ADULT))
    assert raised.value.state == "two_roles" and raised.value.ref == person.ref
    assert harness.roles(person.ref) == {Role.CHILD, Role.ADULT}
    for call in (
        lambda: harness.admin.get_person(person.ref),
        lambda: harness.admin.set_app_access(person.ref, App.IMMICH, True),
        lambda: harness.admin.issue_credential_reset(person.ref),
        lambda: harness.admin.disable(person.ref),
        lambda: harness.admin.set_role(person.ref, Role.OWNER),
    ):
        with pytest.raises(RoleInvariantError):
            run(call())
    assert run(harness.admin.set_role(person.ref, Role.ADULT, repair=True)).role is Role.ADULT
    assert run(harness.admin.get_person(person.ref)) is not None


def test_a_failed_role_at_create_removes_the_person_again(harness: Harness) -> None:
    harness.fail("create_role")
    with pytest.raises(IdentityUnavailable):
        make(harness)
    make(harness)  # the name is free again: nothing was left behind


def test_a_failed_removal_at_create_leaves_a_person_that_can_be_repaired(harness: Harness) -> None:
    harness.fail("create_role_and_rollback")
    with pytest.raises(PartialFailure) as raised:
        make(harness)
    assert raised.value.state == "no_role"
    ref = raised.value.ref
    with pytest.raises(RoleInvariantError):
        run(harness.admin.get_person(ref))
    assert run(harness.admin.set_role(ref, Role.CHILD, repair=True)).role is Role.CHILD


def test_two_role_changes_for_one_person_at_once_leave_one_role(harness: Harness) -> None:
    person = make(harness, Role.CHILD)

    async def both() -> None:
        await asyncio.gather(
            harness.admin.set_role(person.ref, Role.ADULT),
            harness.admin.set_role(person.ref, Role.ADMIN),
        )

    run(both())
    # Probe P5: without a lock both ended with two roles, in every one of 20 rounds.
    assert len(harness.roles(person.ref)) == 1
    assert harness.roles(person.ref) <= {Role.ADULT, Role.ADMIN}


# --- app access ----------


def test_app_access_is_given_and_taken_and_idempotent(harness: Harness) -> None:
    person = make(harness)
    assert App.IMMICH in run(harness.admin.set_app_access(person.ref, App.IMMICH, True)).apps
    assert App.IMMICH in run(harness.admin.set_app_access(person.ref, App.IMMICH, True)).apps
    assert run(harness.admin.set_app_access(person.ref, App.IMMICH, False)).apps == frozenset()
    assert run(harness.admin.set_app_access(person.ref, App.IMMICH, False)).apps == frozenset()


def test_app_access_survives_a_role_change(harness: Harness) -> None:
    person = make(harness, apps=[App.IMMICH])
    assert run(harness.admin.set_role(person.ref, Role.ADULT)).apps == {App.IMMICH}


# --- disable and enable ----------


def test_disable_and_enable_are_idempotent_and_show_in_the_person(harness: Harness) -> None:
    person = make(harness)
    off = run(harness.admin.disable(person.ref))
    assert off.enabled is False and off.account_expire is not None
    assert run(harness.admin.disable(person.ref)).enabled is False
    harness.clock.advance(timedelta(minutes=1))
    on = run(harness.admin.enable(person.ref))
    assert on.enabled is True and on.account_expire is None
    assert run(harness.admin.enable(person.ref)).enabled is True


# --- reset links ----------


def test_a_reset_link_has_the_public_host_and_the_default_lifetime(harness: Harness) -> None:
    person = make(harness)
    link = run(harness.admin.issue_credential_reset(person.ref))
    url = urlsplit(link.reveal())
    assert (url.scheme, url.netloc, url.path) == ("https", "id.test.invalid", "/ui/reset")
    (token,) = parse_qs(url.query)["token"]
    assert token and link.expires_at == harness.clock() + timedelta(minutes=15)


def test_a_reset_link_shows_nothing_of_its_url(harness: Harness) -> None:
    person = make(harness)
    link = run(harness.admin.issue_credential_reset(person.ref))
    token = parse_qs(urlsplit(link.reveal()).query)["token"][0]
    shown = [
        repr(link),
        str(link),
        f"{link}",
        f"{link!r}",
        repr([link]),
        repr({"l": link}),
    ]
    for text in shown:
        assert token not in text and "/ui/reset" not in text and "id.test.invalid" not in text
        assert "redacted" in text
    import pickle

    with pytest.raises(TypeError):
        pickle.dumps(link)


@pytest.mark.parametrize("seconds", [0, 1, 299, 3601, 86400, -300])
def test_a_lifetime_outside_five_minutes_to_one_hour_is_refused_not_clamped(
    harness: Harness, seconds: int
) -> None:
    person = make(harness)
    with pytest.raises(InvalidResetTtl):
        run(harness.admin.issue_credential_reset(person.ref, ttl=timedelta(seconds=seconds)))
    if harness.api is not None:
        assert harness.api.intents == {}  # nothing was issued


@pytest.mark.parametrize("seconds", [300, 900, 3600])
def test_the_edges_of_the_range_are_accepted_exactly(harness: Harness, seconds: int) -> None:
    person = make(harness)
    link = run(harness.admin.issue_credential_reset(person.ref, ttl=timedelta(seconds=seconds)))
    assert link.expires_at == harness.clock() + timedelta(seconds=seconds)


def test_a_non_timedelta_lifetime_is_refused(harness: Harness) -> None:
    person = make(harness)
    with pytest.raises(InvalidResetTtl):
        run(harness.admin.issue_credential_reset(person.ref, ttl=900))  # type: ignore[arg-type]


def test_a_disabled_person_gets_no_link(harness: Harness) -> None:
    person = make(harness)
    run(harness.admin.disable(person.ref))
    with pytest.raises(PersonDisabled):
        run(harness.admin.issue_credential_reset(person.ref))
    harness.clock.advance(timedelta(minutes=1))
    run(harness.admin.enable(person.ref))
    assert run(harness.admin.issue_credential_reset(person.ref)) is not None


def test_a_new_link_does_not_cancel_an_older_one_and_a_commit_uses_one_only(
    harness: Harness,
) -> None:
    person = make(harness)
    first = run(harness.admin.issue_credential_reset(person.ref))
    second = run(harness.admin.issue_credential_reset(person.ref))
    assert harness.link_valid(first) and harness.link_valid(second)  # Kanidm cannot revoke
    harness.commit(second)
    assert harness.link_valid(first) and not harness.link_valid(second)


def test_a_link_stops_working_when_it_expires_and_when_the_person_is_deleted(
    harness: Harness,
) -> None:
    person = make(harness)
    short = run(harness.admin.issue_credential_reset(person.ref, ttl=timedelta(minutes=5)))
    long = run(harness.admin.issue_credential_reset(person.ref, ttl=timedelta(hours=1)))
    harness.clock.advance(timedelta(minutes=6))
    assert not harness.link_valid(short) and harness.link_valid(long)
    run(harness.admin.delete(person.ref))
    assert not harness.link_valid(long)


def test_a_link_for_a_person_that_does_not_exist_is_refused(harness: Harness) -> None:
    ref = PersonRef("5b0a1f4e-0000-4000-8000-000000000001")
    with pytest.raises(PersonNotFound):
        run(harness.admin.issue_credential_reset(ref))


# --- check ----------


def test_check_is_clean_on_a_healthy_setup(harness: Harness) -> None:
    make(harness)
    report = run(harness.admin.check())
    assert report.ok and report.problems == ()


def test_check_reports_a_member_that_is_not_a_person(harness: Harness) -> None:
    make(harness)
    harness.add_stray()
    report = run(harness.admin.check())
    assert not report.ok
    assert any("atlas-console" in p and "not a person" in p for p in report.problems)


# --- nothing secret in a log line, an error or a repr ----------


def test_no_link_or_token_in_any_log_line_error_or_repr(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    person = make(harness)
    link = run(harness.admin.issue_credential_reset(person.ref))
    token = parse_qs(urlsplit(link.reveal()).query)["token"][0]
    run(harness.admin.set_role(person.ref, Role.ADULT))
    run(harness.admin.disable(person.ref))
    harness.clock.advance(timedelta(minutes=1))
    run(harness.admin.enable(person.ref))
    with pytest.raises(InvalidResetTtl) as bad:
        run(harness.admin.issue_credential_reset(person.ref, ttl=timedelta(seconds=1)))
    assert token not in caplog.text and link.reveal() not in caplog.text
    assert token not in str(bad.value) and token not in repr(bad.value)
    assert token not in repr(person) and token not in repr(run(harness.admin.check()))
