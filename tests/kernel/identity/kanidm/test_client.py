"""The Kanidm REST client against scripted responses (PLAN Phase 5b-1).

Statuses and bodies are the ones a real Kanidm 1.12.0 returned in the probe.
"""

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from atlas.kernel.identity.kanidm import (
    ENTRY_ATTRIBUTES,
    ApiTokenInfo,
    Entry,
    KanidmAuthError,
    KanidmClient,
    KanidmConflict,
    KanidmError,
    KanidmInvalidRequest,
    KanidmNotFound,
    KanidmPermissionDenied,
    KanidmProtocolError,
    KanidmServerError,
    KanidmUnavailable,
)
from atlas.kernel.identity.kanidm.client import parse_entry
from tests.kernel.identity.kanidm.fakes import (
    BASE_URL,
    BEARER,
    PASSWORD,
    SESSION,
    LoginScript,
    Recorder,
    body_of,
    fake_secret,
    json_response,
    make_client,
    run,
)

CONFLICT = {"conflicting_attributes": ["name", "spn"], "error": "Attribute uniqueness error"}


def respond(status: int, body: object = None, headers: dict[str, str] | None = None) -> Recorder:
    return Recorder(lambda request: json_response(status, body, headers))


# --- construction ---------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://idp:8443",
        "idp:8443",
        "https://",
        "https://user:pw@idp:8443",
        "https://idp:8443/v1",
        "https://idp:8443/?x=1",
    ],
)
def test_base_url_must_be_plain_https(url: str, ca_file: Path) -> None:
    with pytest.raises(ValueError, match="base_url"):
        KanidmClient(base_url=url, ca_file=ca_file)


def test_a_missing_ca_file_fails_before_any_request(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        KanidmClient(base_url=BASE_URL, ca_file=tmp_path / "missing.pem")


def test_there_is_no_way_to_disable_tls_verification(ca_file: Path) -> None:
    with pytest.raises(TypeError):
        KanidmClient(base_url=BASE_URL, ca_file=ca_file, verify=False)  # type: ignore[call-arg]


def test_environment_proxies_and_redirects_are_not_used(
    ca_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://192.0.2.1:3128")
    monkeypatch.setenv("SSL_CERT_FILE", "/nonexistent/ca.pem")
    client = KanidmClient(base_url=BASE_URL, ca_file=ca_file)
    assert client._http.trust_env is False
    assert client._http.follow_redirects is False
    run(client.aclose())


# --- login ----------------------------------------------------------------------


def test_login_runs_the_three_steps_and_keeps_the_bearer(ca_file: Path) -> None:
    after = Recorder()
    script = LoginScript(then=after)

    async def scenario() -> None:
        async with make_client(script, ca_file) as client:
            await client.login("idm_admin", SecretStr(PASSWORD))
            await client.get_group("atlas_owners")

    run(scenario())
    init, begin, cred = script.steps
    assert [r.method + " " + r.url.path for r in script.steps] == ["POST /v1/auth"] * 3
    assert body_of(init) == {
        "step": {"init2": {"username": "idm_admin", "issue": "token", "privileged": True}}
    }
    assert "x-kanidm-auth-session-id" not in init.headers
    assert body_of(begin) == {"step": {"begin": "password"}}
    assert body_of(cred) == {"step": {"cred": {"password": PASSWORD}}}
    # Each later step echoes the session *header* of the step before.
    assert begin.headers["x-kanidm-auth-session-id"] == SESSION
    assert cred.headers["x-kanidm-auth-session-id"] == SESSION
    # No step is sent with a bearer or with the cookie Kanidm sets.
    assert all("authorization" not in r.headers and "cookie" not in r.headers for r in script.steps)
    assert after.last.headers["authorization"] == f"Bearer {BEARER}"
    assert "cookie" not in after.last.headers


@pytest.mark.parametrize(
    ("denial", "reason"),
    [
        ("incorrect password", "incorrect_password"),
        ("account expired", "account_expired"),
        ("something new", "denied"),
    ],
)
def test_a_refused_login_is_an_auth_error(denial: str, reason: str, ca_file: Path) -> None:
    final = httpx.Response(200, json={"sessionid": "s-1", "state": {"denied": denial}})

    async def scenario() -> None:
        async with make_client(LoginScript(final=final), ca_file) as client:
            await client.login("idm_admin", SecretStr(PASSWORD))

    with pytest.raises(KanidmAuthError) as caught:
        run(scenario())
    assert caught.value.reason == reason
    assert caught.value.operation == "POST /v1/auth"
    assert "something new" not in str(caught.value)


def test_a_disabled_account_is_refused_at_the_first_step(ca_file: Path) -> None:
    handler = respond(200, {"sessionid": "s-1", "state": {"denied": "account expired"}})

    async def scenario() -> None:
        async with make_client(handler, ca_file) as client:
            await client.login("idm_admin", SecretStr(PASSWORD))

    with pytest.raises(KanidmAuthError) as caught:
        run(scenario())
    assert caught.value.reason == "account_expired"
    assert len(handler.requests) == 1


@pytest.mark.parametrize(
    "first",
    [
        httpx.Response(200, json={"sessionid": "s-1", "state": {"choose": ["passkey"]}}),
        # No session header.
        httpx.Response(200, json={"sessionid": "s-1", "state": {"choose": ["password"]}}),
        httpx.Response(200, json={"state": {"choose": ["password"], "extra": 1}}),
        httpx.Response(200, json=["not", "an", "object"]),
        httpx.Response(200, content=b"<html>not json</html>"),
    ],
)
def test_an_unexpected_login_answer_is_a_protocol_error(
    first: httpx.Response, ca_file: Path
) -> None:
    async def scenario() -> None:
        async with make_client(lambda request: first, ca_file) as client:
            await client.login("idm_admin", SecretStr(PASSWORD))

    with pytest.raises(KanidmProtocolError):
        run(scenario())


def test_a_failed_login_keeps_the_client_anonymous(ca_file: Path) -> None:
    after = Recorder()
    script = LoginScript(password="another-password", then=after)

    async def scenario() -> None:
        async with make_client(script, ca_file) as client:
            with pytest.raises(KanidmAuthError):
                await client.login("idm_admin", SecretStr(PASSWORD))
            await client.get_group("atlas_owners")

    run(scenario())
    assert "authorization" not in after.last.headers


# --- status mapping -------------------------------------------------------------


@pytest.mark.parametrize(
    ("response", "error", "detail"),
    [
        (httpx.Response(403, json="accessdenied"), KanidmPermissionDenied, None),
        (httpx.Response(404, json="nomatchingentries"), KanidmNotFound, '"nomatchingentries"'),
        (httpx.Response(409, json=CONFLICT), KanidmConflict, "Attribute uniqueness error"),
        (
            httpx.Response(400, json={"schemaviolation": {"invalidattributesyntax": "name"}}),
            KanidmInvalidRequest,
            "invalidattributesyntax",
        ),
        (
            httpx.Response(422, text="Failed to deserialize the JSON body into the target type"),
            KanidmInvalidRequest,
            "Failed to deserialize",
        ),
        (
            httpx.Response(
                500,
                json={"plugin": {"referentialintegrity": "Uuid referenced not found in database"}},
            ),
            KanidmServerError,
            "referentialintegrity",
        ),
        (httpx.Response(503, text="unavailable"), KanidmServerError, "unavailable"),
        (httpx.Response(404, text="Route not found"), KanidmProtocolError, None),
        (httpx.Response(405), KanidmProtocolError, None),
        (httpx.Response(204), KanidmProtocolError, None),
        (
            httpx.Response(302, headers={"location": "https://example.com/"}),
            KanidmProtocolError,
            None,
        ),
        (httpx.Response(200, content=b"not json"), KanidmProtocolError, None),
    ],
)
def test_statuses_map_to_errors(
    response: httpx.Response, error: type[KanidmError], detail: str | None, ca_file: Path
) -> None:
    async def scenario() -> None:
        async with make_client(lambda request: response, ca_file, token=BEARER) as client:
            await client.add_group_members("atlas_owners", ["someone"])

    with pytest.raises(KanidmError) as caught:
        run(scenario())
    assert type(caught.value) is error
    assert caught.value.status == response.status_code
    assert caught.value.operation == "POST /v1/group/{id}/_attr/{attr}"
    if detail is None:
        assert caught.value.detail is None
    else:
        assert detail in (caught.value.detail or "")
        assert detail in str(caught.value)


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ("notauthenticated", "not_authenticated"),
        ("sessionexpired", "session_expired"),
        ("somethingelse", "not_authenticated"),
        ({"unexpected": "shape"}, "not_authenticated"),
    ],
)
def test_401_is_an_auth_error(body: object, reason: str, ca_file: Path) -> None:
    handler = respond(401, body, headers={"www-authenticate": "Bearer"})

    async def scenario() -> None:
        async with make_client(handler, ca_file, token=BEARER) as client:
            await client.get_group("atlas_owners")

    with pytest.raises(KanidmAuthError) as caught:
        run(scenario())
    assert (caught.value.status, caught.value.reason) == (401, reason)


def test_conflict_names_the_attributes(ca_file: Path) -> None:
    async def scenario() -> None:
        async with make_client(respond(409, CONFLICT), ca_file, token=BEARER) as client:
            await client.create_group("atlas_owners")

    with pytest.raises(KanidmConflict) as caught:
        run(scenario())
    assert caught.value.conflicting_attributes == ("name", "spn")


def test_error_detail_is_cut_to_200_characters_and_never_holds_the_bearer(ca_file: Path) -> None:
    echo = f"bad request with {BEARER} " + "x" * 500

    async def scenario() -> None:
        async with make_client(respond(400, echo), ca_file, token=BEARER) as client:
            await client.create_group("atlas_owners")

    with pytest.raises(KanidmInvalidRequest) as caught:
        run(scenario())
    assert caught.value.detail is not None and len(caught.value.detail) == 200
    assert BEARER not in str(caught.value)
    assert "<redacted>" in caught.value.detail


@pytest.mark.parametrize(
    ("raised", "tls"),
    [
        (httpx.ConnectError("connection refused"), False),
        (httpx.ConnectTimeout("timed out"), False),
        (httpx.ReadTimeout("timed out"), False),
        (httpx.RemoteProtocolError("server disconnected"), False),
    ],
)
def test_transport_failures_are_unavailable(raised: Exception, tls: bool, ca_file: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise raised

    async def scenario() -> None:
        async with make_client(handler, ca_file, token=BEARER) as client:
            await client.get_group("atlas_owners")

    with pytest.raises(KanidmUnavailable) as caught:
        run(scenario())
    assert caught.value.tls is tls
    assert caught.value.status is None
    # The httpx error holds the request (and its Authorization header): not chained.
    assert caught.value.__cause__ is None and caught.value.__context__ is None


# --- reads ----------------------------------------------------------------------


def test_get_on_a_missing_object_is_none(ca_file: Path) -> None:
    handler = respond(200, None)

    async def scenario() -> tuple[object, object, object, object]:
        async with make_client(handler, ca_file, token=BEARER) as client:
            return (
                await client.get_group("nosuch"),
                await client.get_service_account("nosuch"),
                await client.get_oauth2_client("nosuch"),
                await client.get_oauth2_basic_secret("nosuch"),
            )

    assert run(scenario()) == (None, None, None, None)
    assert [r.url.path for r in handler.requests] == [
        "/v1/group/nosuch",
        "/v1/service_account/nosuch",
        "/v1/oauth2/nosuch",
        "/v1/oauth2/nosuch/_basic_secret",
    ]
    assert all(r.method == "GET" and r.content == b"" for r in handler.requests)


def test_get_group_returns_the_allowlisted_attributes(ca_file: Path) -> None:
    body = {
        "attrs": {
            "class": ["account_policy", "group", "object"],
            "credential_type_minimum": ["mfa"],
            "member": ["atlas_console@id.example.com"],
            "name": ["atlas_owners"],
            "spn": ["atlas_owners@id.example.com"],
            "uuid": ["0d4a0c9e-3a0f-4c53-9f0e-2f1f6f1d2a11"],
        }
    }

    async def scenario() -> Entry | None:
        async with make_client(respond(200, body), ca_file, token=BEARER) as client:
            return await client.get_group("atlas_owners")

    entry = run(scenario())
    assert entry is not None
    assert entry.kind == "group"
    assert entry.name == "atlas_owners"
    assert entry.uuid == "0d4a0c9e-3a0f-4c53-9f0e-2f1f6f1d2a11"
    assert entry.attrs["member"] == ("atlas_console@id.example.com",)
    assert entry.first("description") is None


def test_an_entry_drops_secrets_and_unknown_attributes() -> None:
    basic_secret = fake_secret("oauth2-basic-secret")
    key_data = fake_secret("key-internal-data")
    data = {
        "attrs": {
            "name": ["atlas"],
            "oauth2_rs_origin": ["https://atlas.example.com/auth/callback"],
            "oauth2_rs_basic_secret": [basic_secret],
            "key_internal_data": [key_data],
            "attribute_from_a_future_kanidm": ["surprise"],
        }
    }
    entry = parse_entry("oauth2_client", data)
    assert entry is not None
    assert set(entry.attrs) == {"name", "oauth2_rs_origin"}
    for dropped in (
        "oauth2_rs_basic_secret",
        "key_internal_data",
        "attribute_from_a_future_kanidm",
    ):
        assert dropped not in entry.attrs
        assert entry.first(dropped) is None
    for text in (repr(entry), str(entry), repr(entry.attrs)):
        assert basic_secret not in text and key_data not in text and "surprise" not in text


def test_no_allowlist_names_a_credential() -> None:
    # The two names this finds are account-policy settings ("mfa", a length), not
    # credentials. Anything new that matches must be looked at before it is allowed.
    flagged = {
        attr
        for allowed in ENTRY_ATTRIBUTES.values()
        for attr in allowed
        if any(word in attr for word in ("secret", "key", "credential", "password", "token"))
    }
    assert flagged == {"credential_type_minimum", "auth_password_minimum_length"}


@pytest.mark.parametrize(
    "body",
    [
        "a string",
        ["a", "list"],
        {"no_attrs": {}},
        {"attrs": ["not", "a", "mapping"]},
        {"attrs": {"name": "not-a-list"}},
        {"attrs": {"name": [1, 2]}},
    ],
)
def test_a_malformed_entry_is_a_protocol_error(body: object, ca_file: Path) -> None:
    async def scenario() -> None:
        async with make_client(respond(200, body), ca_file, token=BEARER) as client:
            await client.get_group("atlas_owners")

    with pytest.raises(KanidmProtocolError):
        run(scenario())


# --- the bootstrap's writes -----------------------------------------------------


def test_write_requests(ca_file: Path) -> None:
    handler = Recorder()

    async def scenario() -> None:
        async with make_client(handler, ca_file, token=BEARER) as client:
            await client.create_service_account(
                "atlas_console", display_name="Atlas console", managed_by="idm_admins"
            )
            await client.create_group("atlas_owners")
            await client.add_group_attr("atlas_owners", "class", ["account_policy"])
            await client.set_group_attr("atlas_owners", "credential_type_minimum", ["mfa"])
            await client.add_group_members("idm_people_admins", ["atlas_console"])
            await client.create_oauth2_basic_client(
                "atlas", display_name="Atlas", landing_url="https://atlas.example.com/"
            )
            await client.add_oauth2_redirect_url("atlas", "https://atlas.example.com/auth/callback")
            await client.remove_oauth2_redirect_url("atlas", "https://atlas.example.com/old")
            await client.set_oauth2_scope_map("atlas", "atlas_owners", ["openid", "profile"])

    run(scenario())
    sent = [(r.method, r.url.path, body_of(r)) for r in handler.requests]
    assert sent == [
        (
            "POST",
            "/v1/service_account",
            {
                "attrs": {
                    "name": ["atlas_console"],
                    "displayname": ["Atlas console"],
                    "entry_managed_by": ["idm_admins"],
                }
            },
        ),
        ("POST", "/v1/group", {"attrs": {"name": ["atlas_owners"]}}),
        ("POST", "/v1/group/atlas_owners/_attr/class", ["account_policy"]),
        ("PUT", "/v1/group/atlas_owners/_attr/credential_type_minimum", ["mfa"]),
        ("POST", "/v1/group/idm_people_admins/_attr/member", ["atlas_console"]),
        (
            "POST",
            "/v1/oauth2/_basic",
            {
                "attrs": {
                    "name": ["atlas"],
                    "displayname": ["Atlas"],
                    "oauth2_rs_origin_landing": ["https://atlas.example.com/"],
                    "oauth2_strict_redirect_uri": ["true"],
                }
            },
        ),
        (
            "POST",
            "/v1/oauth2/atlas/_attr/oauth2_rs_origin",
            ["https://atlas.example.com/auth/callback"],
        ),
        ("DELETE", "/v1/oauth2/atlas/_attr/oauth2_rs_origin", ["https://atlas.example.com/old"]),
        ("POST", "/v1/oauth2/atlas/_scopemap/atlas_owners", ["openid", "profile"]),
    ]
    assert all(r.headers["authorization"] == f"Bearer {BEARER}" for r in handler.requests)


def test_api_tokens(ca_file: Path) -> None:
    issued = fake_secret("api-token", 96)
    listing = [
        {
            "account_id": "5d1c1b5e-6d7f-4a53-8a55-0d7b5a3d9c10",
            "token_id": "0b0c0d0e-1111-4222-8333-444455556666",
            "label": "atlas-console",
            "expiry": None,
            "issued_at": 1794744000,
            "purpose": "readwrite",
        },
        {
            "account_id": "5d1c1b5e-6d7f-4a53-8a55-0d7b5a3d9c10",
            "token_id": "0b0c0d0e-1111-4222-8333-777788889999",
            "label": "read",
            "expiry": 1798761600,
            "issued_at": 1794744001,
            "purpose": "readonly",
        },
    ]

    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=issued if request.method == "POST" else listing)

    handler = Recorder(answer)
    expiry = datetime(2027, 1, 1, 7, 0, tzinfo=timezone(timedelta(hours=7)))

    async def scenario() -> tuple[SecretStr, SecretStr, list[ApiTokenInfo]]:
        async with make_client(handler, ca_file, token=BEARER) as client:
            return (
                await client.issue_api_token(
                    "atlas_console", label="atlas-console", read_write=True
                ),
                await client.issue_api_token(
                    "atlas_console", label="read", read_write=False, expiry=expiry
                ),
                await client.list_api_tokens("atlas_console"),
            )

    first, _, tokens = run(scenario())
    assert isinstance(first, SecretStr) and first.get_secret_value() == issued
    assert issued not in repr(first)
    assert [body_of(r) for r in handler.requests[:2]] == [
        {"label": "atlas-console", "expiry": None, "read_write": True, "compact": False},
        {"label": "read", "expiry": 1798761600, "read_write": False, "compact": False},
    ]
    assert handler.requests[0].url.path == "/v1/service_account/atlas_console/_api_token"
    assert [(t.label, t.read_write, t.expiry) for t in tokens] == [
        ("atlas-console", True, None),
        ("read", False, datetime(2027, 1, 1, tzinfo=UTC)),
    ]
    assert tokens[0].token_id == "0b0c0d0e-1111-4222-8333-444455556666"
    assert tokens[0].issued_at == datetime(2026, 11, 15, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "listing",
    [
        {"not": "a list"},
        ["not an object"],
        [{"token_id": "x", "label": "y"}],
        [{"token_id": "x", "label": "y", "purpose": "readonly", "issued_at": "2026-11-15"}],
        [{"token_id": "x", "label": "y", "purpose": "readonly", "issued_at": 1, "expiry": "soon"}],
        [{"token_id": "x", "label": "y", "purpose": "readonly", "issued_at": True}],
        "a string",
    ],
)
def test_a_malformed_token_list_is_a_protocol_error(listing: object, ca_file: Path) -> None:
    async def scenario() -> None:
        async with make_client(respond(200, listing), ca_file, token=BEARER) as client:
            await client.list_api_tokens("atlas_console")

    with pytest.raises(KanidmProtocolError):
        run(scenario())


def test_basic_secret_is_a_secretstr(ca_file: Path) -> None:
    secret = fake_secret("basic-secret")

    async def scenario() -> SecretStr | None:
        async with make_client(respond(200, secret), ca_file, token=BEARER) as client:
            return await client.get_oauth2_basic_secret("atlas")

    result = run(scenario())
    assert isinstance(result, SecretStr) and result.get_secret_value() == secret


# --- argument validation --------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["", "Atlas", "atlas owners", "../admin", "atlas/owners", "a" * 65, "1group", "ätlas"]
)
def test_names_are_validated_before_any_request(name: str, ca_file: Path) -> None:
    handler = Recorder()

    async def scenario() -> None:
        async with make_client(handler, ca_file, token=BEARER) as client:
            for call in (
                client.get_group(name),
                client.create_group(name),
                client.get_service_account(name),
                client.get_oauth2_basic_secret(name),
                client.set_oauth2_scope_map("atlas", name, ["openid"]),
                client.add_group_members(name, ["someone"]),
                client.login(name, SecretStr(PASSWORD)),
            ):
                with pytest.raises(ValueError):
                    await call

    run(scenario())
    assert handler.requests == []


def test_other_arguments_are_validated(ca_file: Path) -> None:
    handler = Recorder()

    async def scenario() -> None:
        async with make_client(handler, ca_file, token=BEARER) as client:
            for call in (
                client.add_group_attr("atlas_owners", "Bad-Attr", ["x"]),
                client.add_group_members("atlas_owners", []),
                client.add_group_members("atlas_owners", "atlas_console"),
                client.set_oauth2_scope_map("atlas", "atlas_owners", [""]),
                client.add_oauth2_redirect_url("atlas", "http://atlas.example.com/callback"),
                client.create_oauth2_basic_client(
                    "atlas", display_name="Atlas", landing_url="atlas.example.com"
                ),
                client.issue_api_token("atlas_console", label="", read_write=True),
                client.issue_api_token(
                    "atlas_console", label="x", read_write=True, expiry=datetime(2027, 1, 1)
                ),
            ):
                with pytest.raises(ValueError):
                    await call

    run(scenario())
    assert handler.requests == []
