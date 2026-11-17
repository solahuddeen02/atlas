"""`KanidmClient.remove_group_members`, added for the IdP bootstrap (PLAN Phase 5b-3a).

Behaviour of Kanidm 1.12.0 (checked against the pinned image): `DELETE
/v1/group/{id}/_attr/member` with a list of names removes those members and is 200 again
for a name that is not a member. Without a body it would remove every member.
"""

from pathlib import Path

import pytest

from atlas.kernel.identity.kanidm import KanidmClient, KanidmNotFound, KanidmPermissionDenied
from tests.kernel.identity.kanidm.fakes import (
    BEARER,
    Recorder,
    body_of,
    json_response,
    make_client,
    run,
)


def call_with(handler: Recorder, ca_file: Path, call):  # type: ignore[no-untyped-def]
    async def scenario() -> object:
        async with make_client(handler, ca_file, token=BEARER) as client:
            return await call(client)

    return run(scenario())


def test_removing_members_is_one_delete_with_the_names_as_the_body(ca_file: Path) -> None:
    handler = Recorder()
    call_with(
        handler, ca_file, lambda c: c.remove_group_members("atlas_role_guest", ["atlas-console"])
    )
    request = handler.last
    assert (request.method, request.url.path) == (
        "DELETE",
        "/v1/group/atlas_role_guest/_attr/member",
    )
    assert body_of(request) == ["atlas-console"]
    assert request.headers["authorization"] == f"Bearer {BEARER}"
    assert len(handler.requests) == 1


@pytest.mark.parametrize("members", [[], [""], "atlas-console"])
def test_no_members_or_a_bare_string_is_refused_before_any_request(
    members: object, ca_file: Path
) -> None:
    # An empty body would make Kanidm remove the whole attribute: never sent.
    handler = Recorder()

    async def call(client: KanidmClient) -> None:
        await client.remove_group_members("atlas_role_guest", members)  # type: ignore[arg-type]

    with pytest.raises(ValueError):
        call_with(handler, ca_file, call)
    assert handler.requests == []


def test_a_group_can_be_given_by_its_uuid_and_a_bad_id_is_refused(ca_file: Path) -> None:
    handler = Recorder()
    uuid = "-".join(["96d5f73e", "4759", "4f79", "9f77", "7bbf44a2f34b"])
    call_with(handler, ca_file, lambda c: c.remove_group_members(uuid, ["someone"]))
    assert handler.last.url.path == f"/v1/group/{uuid}/_attr/member"

    handler = Recorder()
    with pytest.raises(ValueError):
        call_with(handler, ca_file, lambda c: c.remove_group_members("a/b", ["someone"]))
    assert handler.requests == []


def test_removing_a_non_member_twice_is_not_an_error(ca_file: Path) -> None:
    handler = Recorder()

    async def calls(client: KanidmClient) -> None:
        await client.remove_group_members("atlas_role_guest", ["atlas-console"])
        await client.remove_group_members("atlas_role_guest", ["atlas-console"])

    call_with(handler, ca_file, calls)
    assert [r.method for r in handler.requests] == ["DELETE", "DELETE"]


@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        (404, {"nomatchingentries": None}, KanidmNotFound),
        (403, "accessdenied", KanidmPermissionDenied),
    ],
)
def test_removal_errors(status: int, body: object, error: type[Exception], ca_file: Path) -> None:
    handler = Recorder(lambda request: json_response(status, body))
    with pytest.raises(error):
        call_with(handler, ca_file, lambda c: c.remove_group_members("atlas_role_guest", ["x"]))
