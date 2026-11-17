"""Bootstrap version 2: the console manages its six groups and leaves idm_group_admins
(PLAN Phase 5b-3a, ADR-001 "Provisioning").

The order is the point: set the manager on all six groups, read every one back, and only
then remove the console from idm_group_admins. A failure in between fails the run (the
record stays incomplete) and the next run resumes. The real Kanidm is covered by
tests/compose/test_idp_console_rights.py.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from atlas.kernel.identity.bootstrap import BootstrapError
from atlas.kernel.identity.kanidm import KanidmServerError
from atlas.kernel.identity.record import BOOTSTRAP_VERSION
from tests.kernel.identity.fake_idp import DOMAIN, FakeIdp, FakeSocket, Trace, stable_uuid
from tests.kernel.identity.test_bootstrap import ROLE_GROUPS, Rig

SIX = [*ROLE_GROUPS, "immich_users"]
CONSOLE = f"atlas-console@{DOMAIN}"


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    (tmp_path / "atlas-idp").mkdir(mode=0o750)
    (tmp_path / "idp-clients").mkdir(mode=0o700)
    trace = Trace()
    idp = FakeIdp(trace)
    return Rig(tmp_path, idp, FakeSocket(idp), trace)


def make_pre_3a_install(rig: Rig) -> None:
    """What a 5b-2b install looks like: the console is in idm_group_admins, nothing is
    managed by it, and the record says version 1."""
    rig.run()
    rig.idp.groups["idm_group_admins"]["member"].add(CONSOLE)  # type: ignore[attr-defined]
    for group in SIX:
        rig.idp.groups[group].pop("managed_by", None)
    record = rig.state_dir / "bootstrap.json"
    record.write_text(json.dumps({**rig.state, "version": 1}))
    rig.trace.clear()


def in_group_admins(rig: Rig) -> bool:
    return CONSOLE in rig.idp.groups["idm_group_admins"]["member"]  # type: ignore[operator]


def managed(rig: Rig) -> list[str]:
    uuid = rig.idp.accounts["atlas-console"]["uuid"]
    return [g for g in SIX if g in rig.idp.groups and rig.idp.groups[g].get("managed_by") == uuid]


def join(rig: Rig, group: str, *members: str) -> None:
    """Put members into a group as someone with Kanidm access did, outside the bootstrap."""
    for member in members:
        rig.idp.groups[group]["member"].add(f"{member}@{DOMAIN}")  # type: ignore[attr-defined]


def test_the_version_is_two() -> None:
    assert BOOTSTRAP_VERSION == 2


# --- the upgrade from a 5b-2b install ---------------------------------------------


def test_an_install_from_before_this_change_is_upgraded_by_one_full_run(rig: Rig) -> None:
    make_pre_3a_install(rig)

    result = rig.run()

    assert result.skipped is False
    assert "the bootstrap changed since the last run" in rig.lines[0]
    assert managed(rig) == SIX
    assert in_group_admins(rig) is False
    assert "group idm_group_admins member atlas-console removed" in rig.lines
    assert rig.state["complete"] is True and rig.state["version"] == BOOTSTRAP_VERSION
    assert rig.idp.admin_enabled is False


def test_the_order_is_set_all_six_then_read_all_six_back_then_remove(rig: Rig) -> None:
    make_pre_3a_install(rig)
    rig.run()
    trace = list(rig.trace)

    sets = [i for i, t in enumerate(trace) if t.startswith("idp.set_managed_by:")]
    assert [trace[i].split(":")[1] for i in sets] == SIX
    removal = trace.index("idp.removed_members:idm_group_admins")
    # Between the last write and the removal, each of the six is read again.
    read_back = [t for t in trace[sets[-1] + 1 : removal] if t.startswith("idp.get_group:")]
    assert [t.split(":")[1] for t in read_back][: len(SIX)] == SIX
    assert sets[-1] < removal


def test_a_second_run_after_the_upgrade_takes_the_fast_path_and_writes_nothing(rig: Rig) -> None:
    make_pre_3a_install(rig)
    rig.run()
    mark = len(rig.trace)

    result = rig.run()

    assert result.skipped is True
    later = rig.trace[mark:]
    assert not [t for t in later if t.startswith(("idp.set_", "idp.removed_", "idp.add_"))]
    # The console token cannot see atlas_users any more, so the fast path never asks it to.
    assert "idp.get_group:atlas_users" not in later


def test_a_fresh_install_never_joins_idm_group_admins_and_removes_nothing(rig: Rig) -> None:
    rig.run()
    assert "group idm_group_admins member atlas-console absent" in rig.lines
    assert rig.trace.count_prefix("idp.removed_members") == 0
    assert rig.trace.count_prefix("idp.add_group_members:idm_group_admins") == 0
    assert managed(rig) == SIX


def test_without_immich_five_groups_are_managed(rig: Rig) -> None:
    rig.run(immich_url=None)
    assert managed(rig) == ROLE_GROUPS
    assert "immich_users" not in rig.idp.groups


# --- F2: every failure point between the writes and the removal -------------------


def server_error() -> KanidmServerError:
    return KanidmServerError("Kanidm reported an error", operation="PUT /x", status=500)


@pytest.mark.parametrize("group", SIX)
def test_a_failed_manager_write_fails_the_run_and_the_console_stays(rig: Rig, group: str) -> None:
    make_pre_3a_install(rig)
    rig.idp.fail_on[f"set_group_attr:{group}:entry_managed_by"] = server_error()

    with pytest.raises(BootstrapError) as raised:
        rig.run()

    assert raised.value.step == "console"
    assert in_group_admins(rig) is True  # nothing was taken away
    assert rig.state["complete"] is False
    assert rig.idp.admin_enabled is False  # idm_admin is disabled on this way out too
    assert rig.trace.count_prefix("idp.removed_members") == 0


@pytest.mark.parametrize("group", [SIX[0], SIX[-1]])
def test_the_next_run_resumes_and_writes_only_what_is_missing(rig: Rig, group: str) -> None:
    make_pre_3a_install(rig)
    rig.idp.fail_on[f"set_group_attr:{group}:entry_managed_by"] = server_error()
    with pytest.raises(BootstrapError):
        rig.run()
    done = [g for g in SIX if g in managed(rig)]
    rig.trace.clear()

    result = rig.run()

    assert result.skipped is False
    assert managed(rig) == SIX
    assert in_group_admins(rig) is False
    rewritten = [t.split(":")[1] for t in rig.trace if t.startswith("idp.set_managed_by:")]
    assert rewritten == [g for g in SIX if g not in done]  # what was set is not set again
    assert rig.state["complete"] is True


def test_a_write_that_kanidm_ignores_is_caught_by_the_read_back(rig: Rig) -> None:
    make_pre_3a_install(rig)
    real = rig.idp.set_group_attr

    async def ignored(group: str, attr: str, values: Any) -> None:
        if group == "atlas_role_adult" and attr == "entry_managed_by":
            await rig.idp._enter("set_group_attr", group, attr)
            return  # a 200 that changed nothing
        await real(group, attr, values)

    rig.idp.set_group_attr = ignored  # type: ignore[method-assign]

    with pytest.raises(BootstrapError) as raised:
        rig.run()

    assert raised.value.step == "console"
    assert "atlas_role_adult" in raised.value.reason and "not managed" in raised.value.reason
    assert in_group_admins(rig) is True
    assert rig.state["complete"] is False
    assert rig.trace.count_prefix("idp.removed_members") == 0


@pytest.mark.parametrize("group", [SIX[0], SIX[-1]])
def test_a_failed_read_back_fails_the_run_and_the_console_stays(rig: Rig, group: str) -> None:
    make_pre_3a_install(rig)
    real = rig.idp.get_group

    async def second_read_fails(name: str, *, admin: bool = True) -> Any:
        # In the console step the write loop reads each group once and the read-back reads
        # it again: fail that second read (reads before the step do not count).
        started = [
            i for i, t in enumerate(rig.trace) if t == "idp.get_service_account:atlas-console"
        ]
        reads = (
            [t for t in rig.trace[started[-1] :] if t == f"idp.get_group:{name}"] if started else []
        )
        # The trace holds the reads before this one, so the second read sees one.
        if started and name == group and len(reads) == 1:
            raise server_error()
        return await real(name, admin=admin)

    rig.idp.get_group = second_read_fails  # type: ignore[method-assign]

    with pytest.raises(BootstrapError) as raised:
        rig.run()

    assert raised.value.step == "console"
    assert managed(rig) == SIX  # every write was made
    assert in_group_admins(rig) is True  # but not one thing is taken away
    assert rig.state["complete"] is False
    assert rig.trace.count_prefix("idp.removed_members") == 0


def test_a_failed_removal_leaves_the_six_managed_and_the_next_run_finishes(rig: Rig) -> None:
    make_pre_3a_install(rig)
    rig.idp.fail_on["remove_group_members:idm_group_admins"] = server_error()

    with pytest.raises(BootstrapError) as raised:
        rig.run()

    assert raised.value.step == "console"
    assert managed(rig) == SIX and in_group_admins(rig) is True
    assert rig.state["complete"] is False
    rig.trace.clear()

    rig.run()

    assert in_group_admins(rig) is False and rig.state["complete"] is True
    assert rig.trace.count_prefix("idp.set_managed_by") == 0  # already managed: no rewrite


def test_a_removal_that_kanidm_ignores_fails_the_run(rig: Rig) -> None:
    make_pre_3a_install(rig)
    real = rig.idp.remove_group_members

    async def ignored(group: str, members: Any) -> None:
        await rig.idp._enter("remove_group_members", group)  # a 200 that changed nothing

    rig.idp.remove_group_members = ignored  # type: ignore[method-assign]
    with pytest.raises(BootstrapError) as raised:
        rig.run()
    rig.idp.remove_group_members = real  # type: ignore[method-assign]

    assert raised.value.step == "console"
    assert "still a member of idm_group_admins" in raised.value.reason
    assert rig.state["complete"] is False


@pytest.mark.parametrize(
    "call",
    [
        "set_group_attr:atlas_role_guest:entry_managed_by",
        "remove_group_members:idm_group_admins",
        "get_group:idm_group_admins",
    ],
)
def test_a_signal_between_the_writes_and_the_removal_disables_idm_admin(
    rig: Rig, call: str
) -> None:
    make_pre_3a_install(rig)
    rig.idp.fail_on[call] = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        rig.run()

    assert rig.idp.admin_enabled is False
    assert rig.state["complete"] is False
    rig.run()  # the next run resumes
    assert managed(rig) == SIX and in_group_admins(rig) is False
    assert rig.state["complete"] is True


def test_nothing_is_taken_from_idm_group_admins_before_every_group_is_read_back(
    rig: Rig,
) -> None:
    # The same property from the trace of a failing run: no removal call at all.
    make_pre_3a_install(rig)
    rig.idp.fail_on["set_group_attr:immich_users:entry_managed_by"] = server_error()
    with pytest.raises(BootstrapError):
        rig.run()
    assert "idp.removed_members:idm_group_admins" not in rig.trace


# --- the fast path notices a lost manager -----------------------------------------


def test_a_group_the_console_no_longer_manages_blocks_the_fast_path(rig: Rig) -> None:
    rig.run()
    rig.idp.groups["atlas_role_guest"].pop("managed_by")  # someone changed it by hand

    rig.run()

    assert "does not match groups.json" in rig.lines[0]  # the console cannot even see it
    assert managed(rig) == SIX


# --- F1: --verify removes members that are not persons ----------------------------


def test_verify_removes_a_service_account_and_a_group_from_the_six_groups(rig: Rig) -> None:
    rig.run()
    idp = rig.idp
    idp.add_person("alice")
    idp.add_person("bob")
    idp.add_person("dotted.name")  # a name only a person can have
    join(rig, "atlas_role_guest", "alice", "atlas-console")
    join(rig, "immich_users", "bob", "dotted.name", "atlas_role_child")

    rig.run(verify=True)

    assert idp.groups["atlas_role_guest"]["member"] == {f"alice@{DOMAIN}"}
    assert idp.groups["immich_users"]["member"] == {f"bob@{DOMAIN}", f"dotted.name@{DOMAIN}"}
    console_uuid = idp.accounts["atlas-console"]["uuid"]
    child_uuid = idp.groups["atlas_role_child"]["uuid"]
    assert (
        f"group atlas_role_guest member atlas-console {console_uuid} removed (not a person)"
        in rig.lines
    )
    assert (
        f"group immich_users member atlas_role_child {child_uuid} removed (not a person)"
        in rig.lines
    )
    assert "groups hold persons only" not in rig.lines
    # atlas_users holds the five role groups by design and is not one of the six.
    assert idp.groups["atlas_users"]["member"] == {f"{g}@{DOMAIN}" for g in ROLE_GROUPS}


def test_a_run_without_verify_leaves_such_members_alone(rig: Rig) -> None:
    rig.run()
    join(rig, "atlas_role_guest", "atlas-console")
    (rig.state_dir / "bootstrap.json").unlink()  # a full run, but not --verify

    rig.run()

    assert f"atlas-console@{DOMAIN}" in rig.idp.groups["atlas_role_guest"]["member"]


def test_verify_says_so_when_there_is_nothing_to_remove(rig: Rig) -> None:
    rig.run()
    rig.idp.add_person("alice")
    join(rig, "atlas_role_child", "alice")
    rig.run(verify=True)
    assert "groups hold persons only" in rig.lines
    assert rig.trace.count_prefix("idp.removed_members") == 0


def test_the_removal_lines_hold_a_name_and_a_uuid_and_nothing_secret(rig: Rig) -> None:
    rig.run()
    join(rig, "atlas_role_guest", "atlas-console")
    rig.run(verify=True)
    line = next(ln for ln in rig.lines if "(not a person)" in ln)
    assert line.split() == [
        "group",
        "atlas_role_guest",
        "member",
        "atlas-console",
        stable_uuid("account-atlas-console"),
        "removed",
        "(not",
        "a",
        "person)",
    ]
