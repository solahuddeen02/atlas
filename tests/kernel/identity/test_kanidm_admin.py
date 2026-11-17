"""What is specific to the `kanidm` IdentityAdmin adapter (PLAN Phase 5b-3b).

The shared behaviour is in test_admin_contract.py. Here: groups found by UUID and
resolved to their current SPN, a service account's UUID never a person, the reset link
built from the public URL with the token encoded, the error mapping, `check()`, groups.json,
and what the logs and errors hold.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Coroutine
from datetime import timedelta
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import SecretStr

from atlas.kernel.identity.admin import (
    ROLE_GROUP_NAMES,
    IdentityConfigError,
    IdentityPermissionDenied,
    IdentityRejected,
    IdentityUnavailable,
    PersonNotFound,
    PersonRef,
    Role,
    RoleInvariantError,
)
from atlas.kernel.identity.kanidm import (
    KanidmAuthError,
    KanidmInvalidRequest,
    KanidmPermissionDenied,
    KanidmUnavailable,
    ResetIntent,
)
from atlas.kernel.identity.kanidm.admin import KanidmIdentityAdmin, RoleGroups
from tests.kernel.identity.fake_kanidm_api import DOMAIN, FakeKanidmApi

T = TypeVar("T")
PUBLIC = "https://id.test.invalid"


def run(coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


class Clock:
    """A monotonic clock the test moves, for the 30 s cache of group SPNs."""

    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value


def setup(**kw: Any) -> tuple[KanidmIdentityAdmin, FakeKanidmApi, Clock]:
    api = FakeKanidmApi()
    clock = Clock()
    admin = KanidmIdentityAdmin(
        api,
        RoleGroups.from_mapping(api.groups_json()),
        public_url=PUBLIC,
        now=api.clock,
        monotonic=clock,
        **kw,
    )
    return admin, api, clock


def person(admin: KanidmIdentityAdmin, role: Role = Role.CHILD, name: str = "somchai") -> Any:
    return run(admin.create_person(name, "Somchai", role))


# --- groups are found by UUID -------------------------------------------------------------


def test_every_group_call_uses_the_uuid_never_the_name() -> None:
    admin, api, _ = setup()
    p = person(admin)
    run(admin.set_role(p.ref, Role.ADULT))
    uuids = set(api.groups)
    for call in api.calls:
        method, _, arg = call.partition(":")
        if method in ("add_group_members", "remove_group_members"):
            assert arg in uuids, call  # a UUID, not "atlas_role_adult"


def test_a_renamed_role_group_still_maps_by_uuid() -> None:
    admin, api, clock = setup()
    p = person(admin, Role.OWNER)
    api.rename_group("atlas_role_owner", "something_else")
    clock.value += 31  # the cached SPN is old: it is looked up again, by UUID
    assert run(admin.get_person(p.ref)).role is Role.OWNER  # type: ignore[union-attr]


def test_a_renamed_group_is_found_even_inside_the_cache_window() -> None:
    admin, api, _ = setup()
    p = person(admin, Role.OWNER)
    api.rename_group("atlas_role_owner", "something_else")
    # The stale SPN reads as "no role" once; the adapter looks the SPNs up again and the
    # person is found in the group of that UUID.
    assert run(admin.get_person(p.ref)).role is Role.OWNER  # type: ignore[union-attr]


def test_a_look_alike_name_with_another_uuid_never_counts() -> None:
    admin, api, clock = setup()
    p = person(admin, Role.CHILD)
    # The real group is renamed away, and another group takes the name `atlas_role_owner`.
    api.rename_group("atlas_role_owner", "the_real_owner_group")
    impostor = api.add_group("atlas_role_owner")
    api.groups[impostor]["member"].add(f"somchai@{DOMAIN}")  # type: ignore[attr-defined]
    clock.value += 31
    found = run(admin.get_person(p.ref))
    assert found is not None and found.role is Role.CHILD  # the impostor is not the owner group
    # and a person only in the impostor group has no role at all
    api.groups[api.group_uuid("atlas_role_child")]["member"].discard(f"somchai@{DOMAIN}")  # type: ignore[attr-defined]
    with pytest.raises(RoleInvariantError) as raised:
        run(admin.get_person(p.ref))
    assert raised.value.roles == ()


def test_a_group_missing_from_the_idp_is_a_config_error_that_names_the_bootstrap() -> None:
    admin, api, clock = setup()
    p = person(admin)
    del api.groups[api.group_uuid("atlas_role_guest")]
    clock.value += 31  # the cached SPN has expired: the group is looked up again
    with pytest.raises(IdentityConfigError) as raised:
        run(admin.get_person(p.ref))
    assert "atlas_role_guest" in str(raised.value) and "re-run the IdP bootstrap" in str(
        raised.value
    )


# --- a service account is never a person ----------------------------------------------------


def test_the_uuid_of_a_service_account_is_not_a_person() -> None:
    admin, api, _ = setup()
    ref = PersonRef(api.accounts["atlas-console"])
    assert run(admin.get_person(ref)) is None
    assert run(admin.delete(ref)) is False
    assert not [c for c in api.calls if c.startswith("delete_person")]
    with pytest.raises(PersonNotFound):
        run(admin.set_role(ref, Role.OWNER))
    with pytest.raises(PersonNotFound):
        run(admin.issue_credential_reset(ref))
    with pytest.raises(PersonNotFound):
        run(admin.disable(ref))
    assert api.accounts  # the account is untouched


def test_the_uuid_of_a_group_is_not_a_person_either() -> None:
    admin, api, _ = setup()
    ref = PersonRef(api.group_uuid("atlas_users"))
    assert run(admin.get_person(ref)) is None
    assert run(admin.delete(ref)) is False


def test_an_entry_whose_uuid_is_not_the_one_asked_for_is_not_returned() -> None:
    admin, api, _ = setup()
    p = person(admin)
    real = api.get_person

    async def liar(ident: str) -> Any:
        entry = await real(ident)
        return (
            None
            if entry is None
            else type(entry)(
                kind="person",
                attrs={**entry.attrs, "uuid": ("0" * 8 + "-0000-4000-8000-" + "0" * 12,)},
            )
        )

    api.get_person = liar  # type: ignore[method-assign]
    assert run(admin.get_person(p.ref)) is None


# --- the reset link --------------------------------------------------------------------------


def test_the_link_is_built_from_the_public_url_and_the_token_is_encoded() -> None:
    admin, api, _ = setup()
    p = person(admin)
    weird = "a b/c+d=e&f#g?h%i"

    async def issue(ident: str, *, ttl_seconds: int) -> ResetIntent:
        return ResetIntent(token=SecretStr(weird), expiry=api.clock())

    api.issue_credential_reset_intent = issue  # type: ignore[method-assign]
    link = run(admin.issue_credential_reset(p.ref))
    url = link.reveal()
    assert url.startswith("https://id.test.invalid/ui/reset?token=")
    assert " " not in url and "#" not in url and "&" not in url.split("?", 1)[1]
    assert parse_qs(urlsplit(url).query)["token"] == [weird]  # and it decodes back


def test_the_link_never_uses_an_internal_address() -> None:
    admin, _api, _ = setup()
    link = run(admin.issue_credential_reset(person(admin).ref))
    assert "idp" not in urlsplit(link.reveal()).hostname.split(".")[0:1]  # type: ignore[union-attr]
    assert urlsplit(link.reveal()).netloc == "id.test.invalid"


@pytest.mark.parametrize(
    "bad",
    [
        "http://id.test.invalid",
        "id.test.invalid",
        "https://id.test.invalid/x",
        "https://u:p@id.test.invalid",
        "https://",
    ],
)
def test_the_public_url_must_be_an_https_origin(bad: str) -> None:
    api = FakeKanidmApi()
    with pytest.raises(ValueError):
        KanidmIdentityAdmin(api, RoleGroups.from_mapping(api.groups_json()), public_url=bad)


def test_a_port_in_the_public_url_is_kept() -> None:
    api = FakeKanidmApi()
    admin = KanidmIdentityAdmin(
        api, RoleGroups.from_mapping(api.groups_json()), public_url="https://id.test.invalid:8443/"
    )
    p = run(admin.create_person("somchai", "Somchai", Role.CHILD))
    assert (
        urlsplit(run(admin.issue_credential_reset(p.ref)).reveal()).netloc == "id.test.invalid:8443"
    )


def test_the_ttl_reaches_kanidm_in_whole_seconds() -> None:
    admin, api, _ = setup()
    p = person(admin)
    seen: list[int] = []
    real = api.issue_credential_reset_intent

    async def spy(ident: str, *, ttl_seconds: int) -> ResetIntent:
        seen.append(ttl_seconds)
        return await real(ident, ttl_seconds=ttl_seconds)

    api.issue_credential_reset_intent = spy  # type: ignore[method-assign]
    run(admin.issue_credential_reset(p.ref))
    run(admin.issue_credential_reset(p.ref, ttl=timedelta(minutes=5)))
    assert seen == [900, 300]


# --- errors ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (
            KanidmPermissionDenied("permission denied", operation="X", status=403),
            IdentityPermissionDenied,
        ),
        (
            KanidmAuthError(
                "not authenticated", operation="X", status=401, reason="not_authenticated"
            ),
            IdentityPermissionDenied,
        ),
        (
            KanidmInvalidRequest("rejected", operation="X", status=400, detail="a reason"),
            IdentityRejected,
        ),
        (KanidmUnavailable("connection failed", operation="GET /x"), IdentityUnavailable),
    ],
)
def test_idp_errors_become_identity_errors_with_a_fixed_text(
    raised: Exception, expected: type
) -> None:
    admin, api, _ = setup()
    p = person(admin)
    api.fail("get_person", [raised])
    with pytest.raises(expected) as caught:
        run(admin.get_person(p.ref))
    assert "a reason" not in str(caught.value)  # the IdP's own text is not passed on


def test_an_error_in_the_reset_call_carries_nothing_of_the_exchange() -> None:
    admin, api, _ = setup()
    p = person(admin)
    secret_in_error = "tok-" + "x" * 20
    api.fail(
        "issue_credential_reset_intent",
        [KanidmInvalidRequest("rejected", operation="GET /x", status=400, detail=secret_in_error)],
    )
    with pytest.raises(IdentityRejected) as caught:
        run(admin.issue_credential_reset(p.ref))
    assert secret_in_error not in str(caught.value) and secret_in_error not in repr(caught.value)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


# --- check ----------


def test_check_names_a_stray_member_with_its_uuid() -> None:
    admin, api, _ = setup()
    group = api.groups[api.group_uuid("atlas_role_guest")]
    group["member"].add(f"atlas-console@{DOMAIN}")  # type: ignore[attr-defined]
    problems = run(admin.check()).problems
    assert problems == (
        f"the group atlas_role_guest has a member that is not a person: atlas-console "
        f"({api.accounts['atlas-console']})",
    )


def test_check_reports_a_nested_group_and_a_name_it_cannot_resolve() -> None:
    admin, api, _ = setup()
    guest = api.groups[api.group_uuid("atlas_role_guest")]
    guest["member"].add(f"atlas_role_child@{DOMAIN}")  # type: ignore[attr-defined]
    guest["member"].add(f"ghost@{DOMAIN}")  # type: ignore[attr-defined]
    problems = run(admin.check()).problems
    assert any(
        "atlas_role_child (" in p and api.group_uuid("atlas_role_child") in p for p in problems
    )
    assert any("ghost (UUID unknown)" in p for p in problems)


def test_check_keeps_persons_with_a_name_only_a_person_has() -> None:
    admin, api, _ = setup()
    api.raw_add_person("dotted.name", ["atlas_role_child"])
    assert run(admin.check()).ok


def test_check_reports_a_group_the_console_cannot_read() -> None:
    admin, api, _ = setup()
    del api.groups[api.group_uuid("immich_users")]
    (problem,) = run(admin.check()).problems
    assert "immich_users" in problem and "re-run the IdP bootstrap" in problem


@pytest.mark.parametrize(
    ("raised", "text"),
    [
        (
            KanidmAuthError(
                "x", operation="GET /v1/group/{id}", status=401, reason="not_authenticated"
            ),
            "refuses the console's token",
        ),
        (KanidmPermissionDenied("x", operation="GET", status=403), "refuses the console's token"),
        (KanidmUnavailable("down", operation="GET /v1/group/{id}"), "cannot be reached"),
    ],
)
def test_check_never_raises_and_says_what_is_wrong(raised: Exception, text: str) -> None:
    admin, api, _ = setup()
    api.fail("get_group", [raised])
    report = run(admin.check())
    assert not report.ok and text in report.problems[0]


# --- groups.json ----------------------------------------------------------------------------------


def test_groups_json_is_loaded_by_name_and_uuid(tmp_path: Path) -> None:
    api = FakeKanidmApi()
    path = tmp_path / "groups.json"
    path.write_text(json.dumps(api.groups_json()))
    groups = RoleGroups.load(path)
    assert groups.roles[Role.OWNER] == api.group_uuid("atlas_role_owner")
    assert groups.apps and groups.managed["atlas_role_guest"] == groups.roles[Role.GUEST]


@pytest.mark.parametrize(
    "content",
    [
        None,
        "not json",
        "[]",
        '{"atlas_role_owner": "x"}',
        json.dumps({n: "nope" for n in ROLE_GROUP_NAMES.values()}),
    ],
)
def test_a_bad_groups_json_is_a_config_error(tmp_path: Path, content: str | None) -> None:
    path = tmp_path / "groups.json"
    if content is not None:
        path.write_text(content)
    with pytest.raises(IdentityConfigError, match=r"groups\.json"):
        RoleGroups.load(path)


def test_one_uuid_for_two_groups_is_refused() -> None:
    api = FakeKanidmApi()
    data = api.groups_json()
    data["atlas_role_admin"] = data["atlas_role_owner"]
    with pytest.raises(IdentityConfigError, match="two groups"):
        RoleGroups.from_mapping(data)


def test_a_missing_immich_group_means_no_app_not_an_error() -> None:
    api = FakeKanidmApi()
    data = api.groups_json()
    del data["immich_users"]
    assert RoleGroups.from_mapping(data).apps == {}


# --- logs and errors ----------


def test_the_logs_hold_a_uuid_and_an_operation_never_a_name_or_a_link(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    admin, api, _ = setup()
    p = run(admin.create_person("somchai", "สมชาย", Role.CHILD))
    link = run(admin.issue_credential_reset(p.ref))
    run(admin.set_role(p.ref, Role.ADULT))
    api.fail(
        "remove_group_members",
        [
            KanidmUnavailable("down", operation="DELETE /x"),
            KanidmUnavailable("down", operation="DELETE /x"),
        ],
    )
    with pytest.raises(Exception):  # noqa: B017 - a PartialFailure; its text is checked below
        run(admin.set_role(p.ref, Role.OWNER))
    text = caplog.text
    token = parse_qs(urlsplit(link.reveal()).query)["token"][0]
    assert token not in text and link.reveal() not in text
    assert "somchai" not in text and "สมชาย" not in text
    assert str(p.ref) in text  # the UUID identifies the person in the log
    assert "set_role person=" in text
