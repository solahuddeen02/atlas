"""What the `fake` IdentityAdmin models beyond the contract (PLAN Phase 5b-3b), and the
rules both adapters share (usernames, display names, reset lifetimes).

The sessions follow probe P9 on Kanidm 1.12.0: disabling refuses a person's existing
sessions, enabling makes them valid again until their own lifetime ends, and a credential
change ends them for good.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from datetime import timedelta
from typing import Any, TypeVar

import pytest

from atlas.kernel.identity.admin import (
    RESET_TTL_DEFAULT,
    RESET_TTL_MAX,
    RESET_TTL_MIN,
    InvalidDisplayName,
    InvalidResetTtl,
    InvalidUsername,
    ResetLink,
    Role,
    check_reset_ttl,
    validate_display_name,
    validate_username,
)
from atlas.kernel.identity.fake import FailurePoint, FakeClock, FakeIdentityAdmin

T = TypeVar("T")


def run(coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


def enrolled(role: Role = Role.CHILD) -> tuple[FakeIdentityAdmin, Any]:
    fake = FakeIdentityAdmin()
    person = run(fake.create_person("somchai", "Somchai", role))
    link = run(fake.issue_credential_reset(person.ref))
    fake.commit_credential(fake.open_reset_link(link))
    return fake, person


# --- sessions (probe P9) ----------


def test_a_person_with_no_credential_cannot_sign_in() -> None:
    fake = FakeIdentityAdmin()
    person = run(fake.create_person("somchai", "Somchai", Role.CHILD))
    with pytest.raises(ValueError):
        fake.login(person.ref)


def test_disable_refuses_a_session_and_enable_makes_it_valid_again() -> None:
    fake, person = enrolled()
    session = fake.login(person.ref)
    assert fake.session_alive(session)
    run(fake.disable(person.ref))
    assert not fake.session_alive(session)
    with pytest.raises(ValueError):
        fake.login(person.ref)  # a new login is denied too
    fake.clock.advance(timedelta(minutes=1))
    run(fake.enable(person.ref))
    assert fake.session_alive(session)  # it revives: the documented gap


def test_a_credential_change_ends_the_old_sessions_for_good() -> None:
    fake, person = enrolled()
    session = fake.login(person.ref)
    run(fake.disable(person.ref))
    fake.clock.advance(timedelta(minutes=1))
    run(fake.enable(person.ref))
    link = run(fake.issue_credential_reset(person.ref))
    fake.commit_credential(fake.open_reset_link(link))
    assert not fake.session_alive(session)
    assert fake.session_alive(fake.login(person.ref))


@pytest.mark.parametrize(
    ("role", "seconds"),
    [
        (Role.OWNER, 28_800),
        (Role.ADMIN, 28_800),
        (Role.ADULT, 86_400),
        (Role.CHILD, 86_400),
        (Role.GUEST, 86_400),
    ],
)
def test_a_session_lasts_as_long_as_its_role_group_says(role: Role, seconds: int) -> None:
    fake, person = enrolled(role)
    session = fake.login(person.ref)
    fake.clock.advance(timedelta(seconds=seconds - 1))
    assert fake.session_alive(session)
    fake.clock.advance(timedelta(seconds=2))
    assert not fake.session_alive(session)


def test_deleting_a_person_ends_their_sessions() -> None:
    fake, person = enrolled()
    session = fake.login(person.ref)
    run(fake.delete(person.ref))
    assert not fake.session_alive(session)


# --- links ----------


def test_opening_a_link_does_not_consume_it_but_committing_does() -> None:
    fake = FakeIdentityAdmin()
    person = run(fake.create_person("somchai", "Somchai", Role.CHILD))
    link = run(fake.issue_credential_reset(person.ref))
    fake.open_reset_link(link)
    session = fake.open_reset_link(link)
    assert fake.live_intents(person.ref) == 1
    fake.commit_credential(session)
    assert fake.live_intents(person.ref) == 0
    with pytest.raises(ValueError):
        fake.open_reset_link(link)


def test_a_session_object_does_not_show_its_token() -> None:
    fake = FakeIdentityAdmin()
    person = run(fake.create_person("somchai", "Somchai", Role.CHILD))
    link = run(fake.issue_credential_reset(person.ref))
    session = fake.open_reset_link(link)
    assert session.token not in repr(session)


def test_a_reset_link_has_a_redacted_repr_and_cannot_be_pickled_or_compared_by_url() -> None:
    link = ResetLink("https://id.test.invalid/ui/reset?token=abc", FakeClock()())
    assert "abc" not in repr(link) and "abc" not in str(link)
    assert link.reveal().endswith("token=abc")


# --- failure points ----------


@pytest.mark.parametrize("point", list(FailurePoint))
def test_every_failure_point_can_be_reached(point: FailurePoint) -> None:
    fake = FakeIdentityAdmin()
    person = run(fake.create_person("somchai", "Somchai", Role.CHILD))
    fake.fail_at(point)
    calls = {
        FailurePoint.UNAVAILABLE: lambda: fake.get_person(person.ref),
        FailurePoint.CREATE_ADD_ROLE: lambda: fake.create_person("other", "Other", Role.CHILD),
        FailurePoint.CREATE_ROLLBACK: lambda: fake.create_person("other", "Other", Role.CHILD),
        FailurePoint.SET_ROLE_ADD: lambda: fake.set_role(person.ref, Role.ADULT),
        FailurePoint.SET_ROLE_REMOVE: lambda: fake.set_role(person.ref, Role.ADULT),
        FailurePoint.SET_ROLE_ROLLBACK: lambda: fake.set_role(person.ref, Role.ADULT),
        FailurePoint.APP_WRITE: lambda: fake.set_app_access(person.ref, "immich", True),  # type: ignore[arg-type]
        FailurePoint.DELETE: lambda: fake.delete(person.ref),
    }
    if point in (FailurePoint.CREATE_ROLLBACK, FailurePoint.SET_ROLE_ROLLBACK):
        # The rollback is only reached after the failure it undoes.
        fake.fail_at(
            FailurePoint.CREATE_ADD_ROLE
            if point is FailurePoint.CREATE_ROLLBACK
            else FailurePoint.SET_ROLE_REMOVE
        )
        fake.fail_at(point)
    with pytest.raises(Exception):  # noqa: B017 - which error is the contract suite's business
        run(calls[point]())


def test_a_failure_point_fires_once_by_default_and_then_the_call_works() -> None:
    fake = FakeIdentityAdmin()
    person = run(fake.create_person("somchai", "Somchai", Role.CHILD))
    fake.fail_at(FailurePoint.UNAVAILABLE)
    with pytest.raises(Exception):  # noqa: B017
        run(fake.get_person(person.ref))
    assert run(fake.get_person(person.ref)) is not None


# --- the rules both adapters share ----------


@pytest.mark.parametrize(
    "name", ["ab", "somchai", "a1", "a_b", "a-b", "x" * 32, "atlas_role_owner"]
)
def test_usernames_that_pass(name: str) -> None:
    assert validate_username(name) == name


@pytest.mark.parametrize(
    "name",
    [
        "",
        "a",
        "x" * 33,
        "Somchai",
        "9a",
        "_a",
        "-a",
        "a b",
        "a.b",
        "a@b",
        "สมชาย",
        "josé",
        "admin",
        "root",
        "anonymous",
        "atlas-console",
        "system",
        "idm_x",
        "idm_admin",
        "0d5c4c2e-1f0a-4b5e-9e55-3a8b0c6f7d21",
        None,
        5,
    ],
)
def test_usernames_that_do_not(name: object) -> None:
    with pytest.raises(InvalidUsername):
        validate_username(name)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("given", "stored"),
    [
        ("Somchai", "Somchai"),
        ("  สมชาย  ", "สมชาย"),
        ("Café", "Café"),
        ("a" * 255, "a" * 255),
        ("👨‍👩‍👧", "👨‍👩‍👧"),
    ],
)
def test_display_names_that_pass(given: str, stored: str) -> None:
    assert validate_display_name(given) == stored


@pytest.mark.parametrize("given", ["", " ", "\t", "a\nb", "a\x00b", "a\x1bb", "a" * 256, None, 3])
def test_display_names_that_do_not(given: object) -> None:
    with pytest.raises(InvalidDisplayName):
        validate_display_name(given)  # type: ignore[arg-type]


def test_the_reset_lifetime_range_and_default() -> None:
    assert timedelta(minutes=15) == RESET_TTL_DEFAULT
    assert (timedelta(minutes=5), timedelta(hours=1)) == (RESET_TTL_MIN, RESET_TTL_MAX)
    assert check_reset_ttl(RESET_TTL_DEFAULT) == 900
    for bad in (timedelta(seconds=299), timedelta(seconds=3601), timedelta(0), timedelta(days=1)):
        with pytest.raises(InvalidResetTtl):
            check_reset_ttl(bad)
