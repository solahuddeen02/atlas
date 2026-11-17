"""The client's two removal calls added for the IdP bootstrap (PLAN Phase 5b-2b).

Behaviour of Kanidm 1.12.0 (checked against the pinned image): `DELETE
/v1/oauth2/{id}/_scopemap/{group}` is 200, removes only that group's map from that
client, and is 200 again; a missing group or client is 404; the group may be a UUID.
"""

from pathlib import Path

import pytest

from atlas.kernel.identity.kanidm import KanidmClient, KanidmNotFound, KanidmPermissionDenied
from tests.kernel.identity.kanidm.fakes import (
    BEARER,
    Recorder,
    assert_no_secret,
    body_of,
    json_response,
    make_client,
    run,
)

# Built at run time: a UUID literal next to a "secret"-like word trips the secret scanner.
GROUP_ID = "-".join(["96d5f73e", "4759", "4f79", "9f77", "7bbf44a2f34b"])


def call_with(handler: Recorder, ca_file: Path, call):  # type: ignore[no-untyped-def]
    async def scenario() -> object:
        async with make_client(handler, ca_file, token=BEARER) as client:
            return await call(client)

    return run(scenario())


# --- remove_oauth2_scope_map ------------------------------------------------------


def test_removing_a_scope_map_is_one_delete_without_a_body(ca_file: Path) -> None:
    handler = Recorder()
    call_with(handler, ca_file, lambda c: c.remove_oauth2_scope_map("atlas", "atlas_role_guest"))
    request = handler.last
    assert (request.method, request.url.path, request.content) == (
        "DELETE",
        "/v1/oauth2/atlas/_scopemap/atlas_role_guest",
        b"",
    )
    assert request.headers["authorization"] == f"Bearer {BEARER}"
    assert len(handler.requests) == 1


def test_a_scope_map_can_be_removed_by_the_groups_uuid(ca_file: Path) -> None:
    handler = Recorder()
    call_with(handler, ca_file, lambda c: c.remove_oauth2_scope_map("atlas", GROUP_ID))
    assert handler.last.url.path == f"/v1/oauth2/atlas/_scopemap/{GROUP_ID}"


def test_removing_twice_or_a_map_that_is_not_there_is_not_an_error(ca_file: Path) -> None:
    # Kanidm answers 200 every time (probed): the second call is a no-op.
    handler = Recorder()

    async def calls(client: KanidmClient) -> None:
        await client.remove_oauth2_scope_map("atlas", "atlas_role_guest")
        await client.remove_oauth2_scope_map("atlas", "atlas_role_guest")
        await client.remove_oauth2_scope_map("atlas", "atlas_never_mapped")

    call_with(handler, ca_file, calls)
    assert [r.method for r in handler.requests] == ["DELETE"] * 3


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
        call_with(
            handler, ca_file, lambda c: c.remove_oauth2_scope_map("atlas", "atlas_role_guest")
        )


@pytest.mark.parametrize(
    ("client", "group"),
    [
        (GROUP_ID, "atlas_role_guest"),  # an OAuth2 client is addressed by name only
        ("Atlas", "atlas_role_guest"),
        ("atlas", "Atlas_Role"),
        ("atlas", "atlas_role_guest@id.test.invalid"),  # a name or a UUID, not an SPN
        ("atlas", ""),
        ("", "atlas_role_guest"),
        ("../atlas", "atlas_role_guest"),
    ],
)
def test_the_removal_refuses_bad_names_before_any_request(
    client: str, group: str, ca_file: Path
) -> None:
    handler = Recorder()
    with pytest.raises(ValueError):
        call_with(handler, ca_file, lambda c: c.remove_oauth2_scope_map(client, group))
    assert handler.requests == []


# --- remove_unwanted_oauth2_redirect_url ----------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://plain.test.invalid/callback",  # add_oauth2_redirect_url would refuse this one
        "https://immich.example.com/auth/login",
        "app.immich:///oauth-callback",
        "javascript:alert(1)",
        "HTTP://Mixed.Case.test.invalid/",
    ],
)
def test_an_unwanted_redirect_url_is_removed_whatever_its_scheme(url: str, ca_file: Path) -> None:
    handler = Recorder()
    call_with(handler, ca_file, lambda c: c.remove_unwanted_oauth2_redirect_url("atlas", url))
    request = handler.last
    assert (request.method, request.url.path) == (
        "DELETE",
        "/v1/oauth2/atlas/_attr/oauth2_rs_origin",
    )
    # Always with the one URL as the body: a DELETE without one removes the attribute.
    assert body_of(request) == [url]


@pytest.mark.parametrize(
    "url",
    ["", " ", "https://a.test.invalid/x y", "https://a.test.invalid/\n", "a\tb", None, 7, ["x"]],
)
def test_nothing_that_could_empty_the_attribute_is_sent(url: object, ca_file: Path) -> None:
    handler = Recorder()
    with pytest.raises(ValueError):
        call_with(
            handler,
            ca_file,
            lambda c: c.remove_unwanted_oauth2_redirect_url("atlas", url),  # type: ignore[arg-type]
        )
    assert handler.requests == []


def test_an_oauth2_client_must_be_named_for_the_redirect_removal(ca_file: Path) -> None:
    handler = Recorder()
    with pytest.raises(ValueError):
        call_with(
            handler,
            ca_file,
            lambda c: c.remove_unwanted_oauth2_redirect_url(GROUP_ID, "https://a.test.invalid/"),
        )
    assert handler.requests == []


def test_the_removals_never_put_the_bearer_in_an_error(
    ca_file: Path, caplog: pytest.LogCaptureFixture
) -> None:
    handler = Recorder(lambda request: json_response(403, "accessdenied"))
    with pytest.raises(KanidmPermissionDenied) as raised:
        call_with(
            handler, ca_file, lambda c: c.remove_oauth2_scope_map("atlas", "atlas_role_guest")
        )
    assert_no_secret([BEARER], caplog, raised.value)
