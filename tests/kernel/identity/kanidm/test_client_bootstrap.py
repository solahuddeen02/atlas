"""The Kanidm client's additions for the IdP bootstrap (PLAN Phase 5b-2).

Statuses, bodies and formats are the ones a real Kanidm 1.12.0 returned in the probe:
custom-scheme redirect URLs, OAuth2 client settings by PATCH, deleting an API token,
and UUIDs as ids.
"""

import base64
import json
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from atlas.kernel.identity.kanidm import (
    ENTRY_ATTRIBUTES,
    KanidmAuthError,
    KanidmClient,
    KanidmError,
    KanidmInvalidRequest,
    KanidmNotFound,
    KanidmPermissionDenied,
    KanidmProtocolError,
    api_token_id,
)
from atlas.kernel.identity.kanidm.client import Entry, parse_entry
from tests.kernel.identity.kanidm.fakes import (
    BEARER,
    PASSWORD,
    Recorder,
    assert_no_secret,
    body_of,
    debug_logging,
    fake_secret,
    json_response,
    make_client,
    run,
)

GROUP_UUID = "96d5f73e-4759-4f79-9f77-7bbf44a2f34b"
CLIENT_UUID = "bc7b3d10-c4f8-4554-b263-a5bc4ede8873"
ACCOUNT_UUID = "d293324c-58bb-47b3-83ed-d66607b9b21d"
LISTED_ID = "42b598e9-f847-42ac-ba2e-6059246e803a"
IDM_ADMINS_UUID = "00000000-0000-0000-0000-000000000001"
APP_REDIRECT = "app.immich:///oauth-callback"

Call = Callable[[KanidmClient], Awaitable[object]]


def respond(status: int, body: object = None) -> Recorder:
    return Recorder(lambda request: json_response(status, body))


def sent(handler: Recorder) -> list[tuple[str, str, object]]:
    return [(r.method, r.url.path, body_of(r)) for r in handler.requests]


def call_with(handler: Recorder, ca_file: Path, call: Call) -> object:
    async def scenario() -> object:
        async with make_client(handler, ca_file, token=BEARER) as client:
            return await call(client)

    return run(scenario())


# --- G1: redirect URLs with an app's own scheme -----------------------------------


def test_a_custom_scheme_redirect_url_is_added_and_removed(ca_file: Path) -> None:
    handler = Recorder()

    async def calls(client: KanidmClient) -> None:
        await client.add_oauth2_redirect_url("immich", APP_REDIRECT)
        await client.remove_oauth2_redirect_url("immich", APP_REDIRECT)
        await client.add_oauth2_redirect_url("immich", "com.example.app:/oauth2redirect")

    call_with(handler, ca_file, calls)
    assert sent(handler) == [
        ("POST", "/v1/oauth2/immich/_attr/oauth2_rs_origin", [APP_REDIRECT]),
        # Always the one URL as the body: without a body, Kanidm removes the attribute.
        ("DELETE", "/v1/oauth2/immich/_attr/oauth2_rs_origin", [APP_REDIRECT]),
        ("POST", "/v1/oauth2/immich/_attr/oauth2_rs_origin", ["com.example.app:/oauth2redirect"]),
    ]


@pytest.mark.parametrize(
    "url",
    [
        # Kanidm 1.12.0 accepts http:// here (200 in the probe): the refusal is ours.
        "http://immich.example.com/auth/login",
        "http://localhost/callback",
        "HTTP://immich.example.com/auth/login",
        "//immich.example.com/auth/login",
        "immich.example.com/auth/login",
        "https://",
        "https://user:pw@immich.example.com/auth/login",
        "",
        # A scheme without a dot is a registered one, or could be: never an app's.
        "immich:///oauth-callback",
        "javascript:alert(1)",
        "data:text/html,x",
        "file:///etc/passwd",
        "ftp://immich.example.com/x",
        "ws://immich.example.com/x",
        # Not a URI at all.
        "app.immich",
        "app.immich:",
        ".immich:///oauth-callback",
        "app.:///oauth-callback",
        "1app.immich:///oauth-callback",
        "App.Immich:///oauth-callback",
        "app.immich:///oauth callback",
        "app.immich:///oauth-callback\n",
        "app.immich:///oauth-callback#fragment",
        "app.immich:///oauth-callbäck",
    ],
)
def test_other_redirect_urls_are_refused_before_any_request(url: str, ca_file: Path) -> None:
    handler = Recorder()

    async def calls(client: KanidmClient) -> None:
        for call in (
            client.add_oauth2_redirect_url("immich", url),
            client.remove_oauth2_redirect_url("immich", url),
        ):
            with pytest.raises(ValueError, match="url must be"):
                await call

    call_with(handler, ca_file, calls)
    assert handler.requests == []


def test_a_redirect_url_must_be_a_string(ca_file: Path) -> None:
    handler = Recorder()

    async def calls(client: KanidmClient) -> None:
        for url in (None, 7, ["https://immich.example.com/auth/login"]):
            with pytest.raises(ValueError):
                await client.add_oauth2_redirect_url("immich", url)  # type: ignore[arg-type]

    call_with(handler, ca_file, calls)
    assert handler.requests == []


def test_a_landing_url_is_still_https_only(ca_file: Path) -> None:
    handler = Recorder()

    async def calls(client: KanidmClient) -> None:
        for call in (
            client.create_oauth2_basic_client(
                "immich", display_name="Immich", landing_url=APP_REDIRECT
            ),
            client.update_oauth2_client("immich", landing_url=APP_REDIRECT),
            client.update_oauth2_client("immich", landing_url="http://immich.example.com/"),
        ):
            with pytest.raises(ValueError, match="landing_url"):
                await call

    call_with(handler, ca_file, calls)
    assert handler.requests == []


# --- G2: OAuth2 client settings ---------------------------------------------------


def test_update_oauth2_client_is_one_patch(ca_file: Path) -> None:
    handler = Recorder()

    async def calls(client: KanidmClient) -> None:
        await client.update_oauth2_client(
            "atlas",
            landing_url="https://atlas.example.com/api/v1/auth/callback",
            strict_redirect_uri=True,
            legacy_crypto=False,
            require_pkce=True,
        )

    call_with(handler, ca_file, calls)
    assert sent(handler) == [
        (
            "PATCH",
            "/v1/oauth2/atlas",
            {
                "attrs": {
                    "oauth2_rs_origin_landing": ["https://atlas.example.com/api/v1/auth/callback"],
                    "oauth2_strict_redirect_uri": ["true"],
                    "oauth2_jwt_legacy_crypto_enable": ["false"],
                    # Kanidm stores the opposite of "PKCE required".
                    "oauth2_allow_insecure_client_disable_pkce": ["false"],
                }
            },
        )
    ]
    assert handler.last.headers["authorization"] == f"Bearer {BEARER}"


@pytest.mark.parametrize(
    ("settings", "attrs"),
    [
        ({"strict_redirect_uri": True}, {"oauth2_strict_redirect_uri": ["true"]}),
        ({"strict_redirect_uri": False}, {"oauth2_strict_redirect_uri": ["false"]}),
        ({"legacy_crypto": True}, {"oauth2_jwt_legacy_crypto_enable": ["true"]}),
        ({"legacy_crypto": False}, {"oauth2_jwt_legacy_crypto_enable": ["false"]}),
        ({"require_pkce": True}, {"oauth2_allow_insecure_client_disable_pkce": ["false"]}),
        ({"require_pkce": False}, {"oauth2_allow_insecure_client_disable_pkce": ["true"]}),
        (
            {"landing_url": "https://atlas.example.com/"},
            {"oauth2_rs_origin_landing": ["https://atlas.example.com/"]},
        ),
    ],
)
def test_update_oauth2_client_sends_only_what_was_given(
    settings: dict[str, object], attrs: dict[str, list[str]], ca_file: Path
) -> None:
    handler = Recorder()
    call_with(handler, ca_file, lambda c: c.update_oauth2_client("atlas", **settings))  # type: ignore[arg-type]
    assert sent(handler) == [("PATCH", "/v1/oauth2/atlas", {"attrs": attrs})]


def test_update_oauth2_client_arguments_are_validated(ca_file: Path) -> None:
    handler = Recorder()

    async def calls(client: KanidmClient) -> None:
        for call in (
            client.update_oauth2_client("atlas"),
            client.update_oauth2_client("Atlas", strict_redirect_uri=True),
            client.update_oauth2_client("atlas", strict_redirect_uri="true"),  # type: ignore[arg-type]
            client.update_oauth2_client("atlas", require_pkce=1),  # type: ignore[arg-type]
            client.update_oauth2_client("atlas", legacy_crypto="false"),  # type: ignore[arg-type]
        ):
            with pytest.raises(ValueError):
                await call
        # Only the four settings exist: no way to name another attribute.
        with pytest.raises(TypeError):
            await client.update_oauth2_client("atlas", oauth2_rs_basic_secret="x")  # type: ignore[call-arg]

    call_with(handler, ca_file, calls)
    assert handler.requests == []


@pytest.mark.parametrize(
    ("response", "error", "detail"),
    [
        (httpx.Response(404, json="nomatchingentries"), KanidmNotFound, "nomatchingentries"),
        (
            httpx.Response(400, json={"invalidattributename": "oauth2_bogus_attr"}),
            KanidmInvalidRequest,
            "invalidattributename",
        ),
        (httpx.Response(403, json="accessdenied"), KanidmPermissionDenied, None),
        # What GET and PUT on /v1/oauth2/{id}/_attr/{attr} answer: not a way in.
        (httpx.Response(405), KanidmProtocolError, None),
    ],
)
def test_update_oauth2_client_errors(
    response: httpx.Response, error: type[KanidmError], detail: str | None, ca_file: Path
) -> None:
    handler = Recorder(lambda request: response)
    with pytest.raises(KanidmError) as caught:
        call_with(handler, ca_file, lambda c: c.update_oauth2_client("atlas", require_pkce=True))
    assert type(caught.value) is error
    assert caught.value.operation == "PATCH /v1/oauth2/{id}"
    assert caught.value.status == response.status_code
    if detail is None:
        assert caught.value.detail is None
    else:
        assert detail in (caught.value.detail or "")


def test_the_new_settings_are_read_and_secrets_still_dropped() -> None:
    assert {
        "oauth2_allow_insecure_client_disable_pkce",
        "oauth2_jwt_legacy_crypto_enable",
    } <= ENTRY_ATTRIBUTES["oauth2_client"]
    basic_secret = fake_secret("oauth2-basic-secret")
    entry = parse_entry(
        "oauth2_client",
        {
            "attrs": {
                "name": ["atlas"],
                "oauth2_allow_insecure_client_disable_pkce": ["false"],
                "oauth2_jwt_legacy_crypto_enable": ["false"],
                "oauth2_strict_redirect_uri": ["true"],
                "oauth2_rs_basic_secret": [basic_secret],
                "key_internal_data": ["x"],
            }
        },
    )
    assert entry is not None
    assert entry.first("oauth2_allow_insecure_client_disable_pkce") == "false"
    assert entry.first("oauth2_jwt_legacy_crypto_enable") == "false"
    assert "oauth2_rs_basic_secret" not in entry.attrs and "key_internal_data" not in entry.attrs
    assert basic_secret not in repr(entry)
    # A new client has neither attribute: that reads as None, not as "false".
    default = parse_entry("oauth2_client", {"attrs": {"name": ["atlas"]}})
    assert default is not None
    assert default.first("oauth2_allow_insecure_client_disable_pkce") is None
    assert default.first("oauth2_jwt_legacy_crypto_enable") is None


def test_the_client_has_no_call_that_deletes_a_whole_attribute(ca_file: Path) -> None:
    # A DELETE on /v1/oauth2/{id}/_attr/{attr} with no body removes the attribute,
    # also oauth2_strict_redirect_uri. Adding a public method means looking at this
    # list, and at whether the method can send such a request.
    public = {name for name in vars(KanidmClient) if not name.startswith("_")}
    assert public == {
        "aclose",
        "add_group_attr",
        "add_group_members",
        "add_oauth2_redirect_url",
        "create_group",
        "create_oauth2_basic_client",
        "create_service_account",
        "delete_api_token",
        "get_group",
        "get_oauth2_basic_secret",
        "get_oauth2_client",
        "get_service_account",
        "issue_api_token",
        "list_api_tokens",
        "login",
        "remove_group_members",
        "remove_oauth2_redirect_url",
        "remove_oauth2_scope_map",
        "remove_unwanted_oauth2_redirect_url",
        "set_group_attr",
        "set_oauth2_scope_map",
        "update_oauth2_client",
    }
    handler = Recorder()

    async def calls(client: KanidmClient) -> None:
        await client.remove_oauth2_redirect_url("atlas", "https://atlas.example.com/old")
        await client.delete_api_token("atlas-console", LISTED_ID)
        await client.remove_group_members("atlas_role_guest", ["atlas-console"])

    call_with(handler, ca_file, calls)
    deletes = [(r.url.path, body_of(r)) for r in handler.requests if r.method == "DELETE"]
    assert deletes == [
        ("/v1/oauth2/atlas/_attr/oauth2_rs_origin", ["https://atlas.example.com/old"]),
        (f"/v1/service_account/atlas-console/_api_token/{LISTED_ID}", None),
        ("/v1/group/atlas_role_guest/_attr/member", ["atlas-console"]),
    ]


# --- G3: deleting an API token -----------------------------------------------------


def test_delete_api_token(ca_file: Path) -> None:
    handler = Recorder()
    deleted = call_with(handler, ca_file, lambda c: c.delete_api_token("atlas-console", LISTED_ID))
    assert deleted is True
    request = handler.last
    assert (request.method, request.url.path, request.content) == (
        "DELETE",
        f"/v1/service_account/atlas-console/_api_token/{LISTED_ID}",
        b"",
    )
    assert request.headers["authorization"] == f"Bearer {BEARER}"


def test_deleting_a_token_that_is_gone_is_false_not_an_error(ca_file: Path) -> None:
    handler = respond(404, "nomatchingentries")
    deleted = call_with(handler, ca_file, lambda c: c.delete_api_token("atlas-console", LISTED_ID))
    assert deleted is False
    assert len(handler.requests) == 1


@pytest.mark.parametrize(
    "token_id",
    [
        "",
        "not-a-uuid",
        "atlas-console",
        LISTED_ID.upper(),
        LISTED_ID + "0",
        f"../{LISTED_ID}",
        None,
    ],
)
def test_a_token_id_must_be_a_uuid(token_id: str, ca_file: Path) -> None:
    handler = Recorder()
    with pytest.raises(ValueError, match="token_id"):
        call_with(handler, ca_file, lambda c: c.delete_api_token("atlas-console", token_id))
    assert handler.requests == []


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (httpx.Response(401, json="notauthenticated"), KanidmAuthError),
        (httpx.Response(403, json="accessdenied"), KanidmPermissionDenied),
        (httpx.Response(400, text="Invalid URL: Cannot parse `token_id`"), KanidmInvalidRequest),
        # Not JSON: no such route, which is not "the token is gone".
        (httpx.Response(404, text="Route not found"), KanidmProtocolError),
    ],
)
def test_delete_api_token_errors(
    response: httpx.Response, error: type[KanidmError], ca_file: Path
) -> None:
    handler = Recorder(lambda request: response)
    with pytest.raises(KanidmError) as caught:
        call_with(handler, ca_file, lambda c: c.delete_api_token("atlas-console", LISTED_ID))
    assert type(caught.value) is error
    assert caught.value.operation == "DELETE /v1/service_account/{id}/_api_token/{token_id}"


def jws(payload: object, *, raw: bytes | None = None) -> str:
    """An unsigned stand-in for a Kanidm API token: header.payload.signature."""

    def part(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    body = raw if raw is not None else json.dumps(payload).encode()
    return ".".join([part(b'{"alg":"ES256"}'), part(body), fake_secret("jws-signature", 86)])


# The payload keys a real token carries.
TOKEN_PAYLOAD = {
    "account_id": ACCOUNT_UUID,
    "expiry": None,
    "issued_at": 1794744000,
    "label": "atlas-console",
    "purpose": "readwrite",
    "token_id": LISTED_ID,
}


def test_api_token_id_reads_the_tokens_own_id() -> None:
    assert api_token_id(SecretStr(jws(TOKEN_PAYLOAD))) == LISTED_ID
    # Any payload length: the base64 padding is restored.
    for label in ("a", "ab", "abc", "abcd"):
        assert api_token_id(SecretStr(jws({**TOKEN_PAYLOAD, "label": label}))) == LISTED_ID


@pytest.mark.parametrize(
    "token",
    [
        "",
        "not-a-token",
        "only.two",
        "a.b.c.d",
        "a.!!!.c",
        "a..c",
        jws(None, raw=b"not json"),
        jws(None, raw=b"\xff\xfe"),
        jws(["token_id", LISTED_ID]),
        jws("a string"),
        jws({"label": "no id"}),
        jws({"token_id": 7}),
        jws({"token_id": None}),
        jws({"token_id": "not-a-uuid"}),
        jws({"token_id": LISTED_ID.upper()}),
        jws({"token_id": f"../{LISTED_ID}"}),
        # A session bearer (what login() returns) names a session, not an API token.
        jws({"session_id": LISTED_ID}),
    ],
)
def test_api_token_id_of_anything_else_is_none(token: str) -> None:
    assert api_token_id(SecretStr(token)) is None


def test_api_token_id_keeps_the_token_out_of_logs(caplog: pytest.LogCaptureFixture) -> None:
    debug_logging(caplog)
    good, bad = jws(TOKEN_PAYLOAD), jws(None, raw=b'{"token_id": ')
    results = (api_token_id(SecretStr(good)), api_token_id(SecretStr(bad)))
    assert results == (LISTED_ID, None)
    secrets = [part for token in (good, bad) for part in token.split(".")]
    assert_no_secret(secrets, caplog, results)


# --- G4: UUIDs as ids ---------------------------------------------------------------


def test_every_id_may_be_a_uuid(ca_file: Path) -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        listing = request.method == "GET" and request.url.path.endswith("/_api_token")
        issuing = request.method == "POST" and request.url.path.endswith("/_api_token")
        return json_response(200, [] if listing else "token" if issuing else None)

    handler = Recorder(answer)

    async def calls(client: KanidmClient) -> None:
        await client.get_group(GROUP_UUID)
        await client.get_service_account(ACCOUNT_UUID)
        await client.list_api_tokens(ACCOUNT_UUID)
        await client.issue_api_token(ACCOUNT_UUID, label="x", read_write=True)
        await client.delete_api_token(ACCOUNT_UUID, LISTED_ID)
        await client.add_group_attr(GROUP_UUID, "class", ["account_policy"])
        await client.set_group_attr(GROUP_UUID, "credential_type_minimum", ["mfa"])
        await client.add_group_members(GROUP_UUID, [ACCOUNT_UUID])
        await client.set_oauth2_scope_map("atlas", GROUP_UUID, ["openid"])
        await client.create_service_account(
            "atlas-console", display_name="Atlas console", managed_by=IDM_ADMINS_UUID
        )

    call_with(handler, ca_file, calls)
    assert [(r.method, r.url.path) for r in handler.requests] == [
        ("GET", f"/v1/group/{GROUP_UUID}"),
        ("GET", f"/v1/service_account/{ACCOUNT_UUID}"),
        ("GET", f"/v1/service_account/{ACCOUNT_UUID}/_api_token"),
        ("POST", f"/v1/service_account/{ACCOUNT_UUID}/_api_token"),
        ("DELETE", f"/v1/service_account/{ACCOUNT_UUID}/_api_token/{LISTED_ID}"),
        ("POST", f"/v1/group/{GROUP_UUID}/_attr/class"),
        ("PUT", f"/v1/group/{GROUP_UUID}/_attr/credential_type_minimum"),
        ("POST", f"/v1/group/{GROUP_UUID}/_attr/member"),
        ("POST", f"/v1/oauth2/atlas/_scopemap/{GROUP_UUID}"),
        ("POST", "/v1/service_account"),
    ]
    members = next(r for r in handler.requests if r.url.path.endswith("/_attr/member"))
    assert body_of(members) == [ACCOUNT_UUID]
    assert body_of(handler.last)["attrs"]["entry_managed_by"] == [IDM_ADMINS_UUID]


def test_an_oauth2_client_is_never_addressed_by_uuid(ca_file: Path) -> None:
    # Probed on Kanidm 1.12.0: by UUID, GET and the secret are `200 null`, PATCH and
    # scope-map writes are 404, and redirect-URL writes are a silent 200 that does
    # nothing. The client refuses the UUID rather than let a write look successful.
    handler = Recorder()

    async def calls(client: KanidmClient) -> None:
        for call in (
            client.get_oauth2_client(CLIENT_UUID),
            client.get_oauth2_basic_secret(CLIENT_UUID),
            client.update_oauth2_client(CLIENT_UUID, strict_redirect_uri=True),
            client.add_oauth2_redirect_url(CLIENT_UUID, APP_REDIRECT),
            client.remove_oauth2_redirect_url(CLIENT_UUID, APP_REDIRECT),
            client.set_oauth2_scope_map(CLIENT_UUID, GROUP_UUID, ["openid"]),
            client.get_oauth2_client(GROUP_UUID),
        ):
            with pytest.raises(ValueError):
                await call

    call_with(handler, ca_file, calls)
    assert handler.requests == []


def test_an_unknown_uuid_reads_as_none(ca_file: Path) -> None:
    # Kanidm: `GET /v1/group/<unknown uuid>` is `200 null`, like an unknown name.
    handler = respond(200, None)
    assert call_with(handler, ca_file, lambda c: c.get_group(GROUP_UUID)) is None


@pytest.mark.parametrize(
    "value",
    [
        GROUP_UUID.upper(),
        GROUP_UUID[:-1],
        GROUP_UUID + "0",
        GROUP_UUID.replace("-", ""),
        "{" + GROUP_UUID + "}",
        f"urn:uuid:{GROUP_UUID}",
        f"{GROUP_UUID}/_attr/member",
        f"../{GROUP_UUID}",
        "96d5f73e-4759-4f79-9f77-7bbf44a2f34g",
        "atlas_users@id.example.com",
    ],
)
def test_ids_are_validated_before_any_request(value: str, ca_file: Path) -> None:
    handler = Recorder()

    async def calls(client: KanidmClient) -> None:
        for call in (
            client.get_group(value),
            client.get_service_account(value),
            client.list_api_tokens(value),
            client.issue_api_token(value, label="x", read_write=True),
            client.delete_api_token(value, LISTED_ID),
            client.set_group_attr(value, "credential_type_minimum", ["mfa"]),
            client.add_group_members(value, ["someone"]),
            client.set_oauth2_scope_map("atlas", value, ["openid"]),
            client.create_service_account("atlas-console", display_name="x", managed_by=value),
        ):
            with pytest.raises(ValueError):
                await call

    call_with(handler, ca_file, calls)
    assert handler.requests == []


def test_a_new_object_gets_a_name_never_a_uuid(ca_file: Path) -> None:
    # A UUID that starts with a letter is also a valid name, so this one starts with a digit.
    handler = Recorder()

    async def calls(client: KanidmClient) -> None:
        for call in (
            client.create_group(GROUP_UUID),
            client.create_service_account(GROUP_UUID, display_name="x", managed_by="idm_admins"),
            client.create_oauth2_basic_client(
                GROUP_UUID, display_name="x", landing_url="https://atlas.example.com/"
            ),
            client.login(GROUP_UUID, SecretStr(PASSWORD)),
        ):
            with pytest.raises(ValueError):
                await call

    call_with(handler, ca_file, calls)
    assert handler.requests == []


def scope_entry(*values: str) -> Entry:
    entry = parse_entry("oauth2_client", {"attrs": {"oauth2_rs_scope_map": list(values)}})
    assert entry is not None
    return entry


def test_scope_maps_parses_what_kanidm_returns() -> None:
    # Set with the group's UUID; Kanidm answered with its SPN, in exactly this form.
    entry = scope_entry('probe_grp@id.example.com: {"openid", "profile"}')
    assert entry.scope_maps == {"probe_grp@id.example.com": frozenset({"openid", "profile"})}
    several = scope_entry(
        'atlas_users@id.example.com: {"groups", "openid", "profile"}',
        'immich_users@id.example.com: {"email"}',
        "empty@id.example.com: {}",
    )
    assert several.scope_maps == {
        "atlas_users@id.example.com": frozenset({"groups", "openid", "profile"}),
        "immich_users@id.example.com": frozenset({"email"}),
        "empty@id.example.com": frozenset(),
    }
    # No scope map at all, and entries of another kind.
    assert scope_entry().scope_maps == {}
    group = parse_entry("group", {"attrs": {"name": ["atlas_users"]}})
    assert group is not None and group.scope_maps == {}


@pytest.mark.parametrize(
    "value",
    [
        "",
        "atlas_users@id.example.com",
        'atlas_users@id.example.com: "openid"',
        'atlas_users@id.example.com: {"openid"',
        "atlas_users@id.example.com: {openid, profile}",
        'atlas_users@id.example.com: {"openid" "profile"}',
        'atlas_users@id.example.com: {"openid", }',
        'atlas_users@id.example.com: {"openid"} trailing',
        ': {"openid"}',
        'two words@id.example.com: {"openid"}',
        '{"atlas_users@id.example.com": ["openid"]}',
    ],
)
def test_an_unknown_scope_map_format_is_an_error_not_skipped(value: str) -> None:
    entry = scope_entry('atlas_users@id.example.com: {"openid"}', value)
    with pytest.raises(KanidmProtocolError) as caught:
        _ = entry.scope_maps
    # The value is not quoted back.
    assert value not in str(caught.value) or value == ""
