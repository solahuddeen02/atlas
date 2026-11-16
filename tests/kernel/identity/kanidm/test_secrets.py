"""No credential reaches a log, an exception or a repr (PLAN Phase 5b-1).

Every test runs with all loggers at DEBUG, including httpx and httpcore, and checks
the captured records plus the str/repr/attributes of each object and of every
exception reachable through __cause__ and __context__.
"""

import http.server
import json
import ssl
import threading
from collections.abc import Callable, Coroutine, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from atlas.kernel.identity.kanidm import (
    AdminSocket,
    KanidmAuthError,
    KanidmClient,
    KanidmError,
    KanidmProtocolError,
    KanidmServerError,
    KanidmUnavailable,
)
from tests.kernel.identity.kanidm.fakes import (
    BEARER,
    COOKIE,
    COOKIE_VALUE,
    PASSWORD,
    SESSION,
    LoginScript,
    Recorder,
    TlsMaterial,
    admin_socket,
    assert_no_secret,
    debug_logging,
    fake_jws,
    fake_secret,
    make_client,
    reply_with,
    run,
)

API_TOKEN = fake_jws("api-token")
CLIENT_SECRET = fake_secret("client-secret")
KEY_DATA = fake_secret("key-internal-data", 96)
LEAK = fake_secret("a-secret-in-an-unexpected-place")
ALL = [PASSWORD, BEARER, SESSION, COOKIE_VALUE, API_TOKEN, CLIENT_SECRET, KEY_DATA, LEAK]

OAUTH2_ENTRY = {
    "attrs": {
        "name": ["atlas"],
        "oauth2_rs_basic_secret": [CLIENT_SECRET],
        "key_internal_data": [KEY_DATA],
    }
}

ClientCall = Callable[[KanidmClient], Coroutine[Any, Any, object]]


def call(
    handler: Callable[[httpx.Request], httpx.Response], ca_file: Path, what: ClientCall
) -> Any:
    async def scenario() -> object:
        async with make_client(handler, ca_file, token=BEARER) as client:
            return await what(client)

    return run(scenario())


def failing(
    handler: Callable[[httpx.Request], httpx.Response],
    ca_file: Path,
    what: ClientCall,
    error: type[KanidmError],
) -> KanidmError:
    with pytest.raises(KanidmError) as caught:
        call(handler, ca_file, what)
    assert type(caught.value) is error
    return caught.value


# --- when everything works ------------------------------------------------------


def test_the_happy_paths_log_and_show_no_secret(
    ca_file: Path, socket_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    debug_logging(caplog)

    def after_login(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/_api_token"):
            return httpx.Response(200, json=API_TOKEN)
        if request.url.path.endswith("/_basic_secret"):
            return httpx.Response(200, json=CLIENT_SECRET)
        return httpx.Response(200, json=OAUTH2_ENTRY)

    async def scenario() -> list[object]:
        reply = b'{"RecoverAccount":{"password":"' + PASSWORD.encode() + b'"}}'
        async with admin_socket(socket_path, reply_with(reply)):
            socket_client = AdminSocket(socket_path, timeout=5)
            password = await socket_client.recover_account("idm_admin")
        async with make_client(LoginScript(then=after_login), ca_file) as client:
            await client.login("idm_admin", password)
            token = await client.issue_api_token("atlas_console", label="x", read_write=True)
            secret = await client.get_oauth2_basic_secret("atlas")
            entry = await client.get_oauth2_client("atlas")
            assert token.get_secret_value() == API_TOKEN
            assert secret is not None and secret.get_secret_value() == CLIENT_SECRET
            return [socket_client, password, client, token, secret, entry, vars(client)]

    objects = run(scenario())
    assert_no_secret(ALL, caplog, *objects)
    # What is logged: the method, the path template and the status.
    messages = [r.getMessage() for r in caplog.records if r.name == "atlas.identity.kanidm"]
    assert any(m.startswith("POST /v1/auth -> 200") for m in messages)
    assert any(m.startswith("GET /v1/oauth2/{id}/_basic_secret -> 200") for m in messages)
    assert "admin socket RecoverAccount -> ok" in messages


# --- secret-bearing responses that are malformed or unexpected --------------------


@pytest.mark.parametrize(
    "final",
    [
        # A success body with an extra field.
        httpx.Response(200, json={"sessionid": "s", "state": {"success": BEARER}, "extra": LEAK}),
        httpx.Response(200, json={"sessionid": "s", "state": {"success": BEARER, "more": LEAK}}),
        httpx.Response(200, json={"sessionid": "s", "state": {"success": [BEARER]}}),
        httpx.Response(200, json={"sessionid": "s", "state": {"unknown": BEARER}}),
        httpx.Response(200, json=[BEARER]),
        httpx.Response(200, content=b"<html>" + BEARER.encode() + b"</html>"),
        httpx.Response(200, content=b'{"state":{"success":"' + BEARER.encode()),  # cut off
    ],
)
def test_an_unexpected_login_answer_carries_nothing_from_the_response(
    final: httpx.Response, ca_file: Path, caplog: pytest.LogCaptureFixture
) -> None:
    debug_logging(caplog)
    error = failing(
        LoginScript(final=final),
        ca_file,
        lambda c: c.login("idm_admin", SecretStr(PASSWORD)),
        KanidmProtocolError,
    )
    assert error.detail is None
    assert (
        str(error) == "POST /v1/auth: unexpected login response (HTTP 200)"
        or str(error) == "POST /v1/auth: the response is not JSON (HTTP 200)"
    )
    assert_no_secret(ALL, caplog, error)


def test_a_login_denial_never_quotes_the_response(
    ca_file: Path, caplog: pytest.LogCaptureFixture
) -> None:
    debug_logging(caplog)
    final = httpx.Response(200, json={"sessionid": "s", "state": {"denied": f"no: {LEAK}"}})
    error = failing(
        LoginScript(final=final),
        ca_file,
        lambda c: c.login("idm_admin", SecretStr(PASSWORD)),
        KanidmAuthError,
    )
    assert isinstance(error, KanidmAuthError) and error.reason == "denied"
    assert_no_secret(ALL, caplog, error)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422, 500])
def test_an_error_status_on_a_secret_bearing_endpoint_has_no_detail(
    status: int, ca_file: Path, caplog: pytest.LogCaptureFixture
) -> None:
    debug_logging(caplog)
    body = {"error": f"echoed {LEAK}", "conflicting_attributes": [LEAK]}
    calls: list[ClientCall] = [
        lambda c: c.login("idm_admin", SecretStr(PASSWORD)),
        lambda c: c.issue_api_token("atlas_console", label="x", read_write=True),
        lambda c: c.get_oauth2_basic_secret("atlas"),
        lambda c: c.get_oauth2_client("atlas"),
    ]
    for what in calls:
        with pytest.raises(KanidmError) as caught:
            call(lambda request: httpx.Response(status, json=body), ca_file, what)
        assert caught.value.detail is None
        assert caught.value.status == status
        assert getattr(caught.value, "conflicting_attributes", ()) == ()
        assert_no_secret(ALL, caplog, caught.value)


@pytest.mark.parametrize(
    "body",
    [{"token": API_TOKEN}, [API_TOKEN], 12345, "", {"attrs": {"name": API_TOKEN}}],
)
def test_a_token_reply_of_the_wrong_type(
    body: object, ca_file: Path, caplog: pytest.LogCaptureFixture
) -> None:
    debug_logging(caplog)
    error = failing(
        lambda request: httpx.Response(200, json=body),
        ca_file,
        lambda c: c.issue_api_token("atlas_console", label="x", read_write=True),
        KanidmProtocolError,
    )
    assert error.detail is None
    assert_no_secret(ALL, caplog, error)


def test_a_token_reply_that_is_not_json(ca_file: Path, caplog: pytest.LogCaptureFixture) -> None:
    debug_logging(caplog)
    error = failing(
        lambda request: httpx.Response(200, content=b'"' + API_TOKEN.encode()),
        ca_file,
        lambda c: c.issue_api_token("atlas_console", label="x", read_write=True),
        KanidmProtocolError,
    )
    assert_no_secret(ALL, caplog, error)


@pytest.mark.parametrize("body", [{"secret": CLIENT_SECRET}, [CLIENT_SECRET], 7])
def test_a_basic_secret_reply_of_the_wrong_type(
    body: object, ca_file: Path, caplog: pytest.LogCaptureFixture
) -> None:
    debug_logging(caplog)
    error = failing(
        lambda request: httpx.Response(200, json=body),
        ca_file,
        lambda c: c.get_oauth2_basic_secret("atlas"),
        KanidmProtocolError,
    )
    assert_no_secret(ALL, caplog, error)


def test_a_malformed_oauth2_entry(ca_file: Path, caplog: pytest.LogCaptureFixture) -> None:
    debug_logging(caplog)
    body = {"attrs": {"name": "atlas", "oauth2_rs_basic_secret": CLIENT_SECRET}}
    error = failing(
        lambda request: httpx.Response(200, json=body),
        ca_file,
        lambda c: c.get_oauth2_client("atlas"),
        KanidmProtocolError,
    )
    assert_no_secret(ALL, caplog, error)


@pytest.mark.parametrize(
    "reply",
    [
        # A complete answer with trailing data.
        b'{"RecoverAccount":{"password":"'
        + PASSWORD.encode()
        + b'"}}{"more":"'
        + LEAK.encode()
        + b'"}',
        b'{"RecoverAccount":{"password":"' + PASSWORD.encode() + b'"}} trailing ' + LEAK.encode(),
        b'{"RecoverAccount":{"password":"' + PASSWORD.encode() + b'","extra":1}}',
        b'{"RecoverAccount":{"password":["' + PASSWORD.encode() + b'"]}}',
        b'{"Unexpected":{"password":"' + PASSWORD.encode() + b'"}}',
        b'"' + PASSWORD.encode() + b'"',
        b'{"RecoverAccount":{"password":"' + PASSWORD.encode(),  # cut off, then silence
        b"garbage " + PASSWORD.encode(),
    ],
)
def test_an_unexpected_socket_reply_carries_no_reply_bytes(
    reply: bytes, socket_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    debug_logging(caplog)

    async def scenario(request: str) -> None:
        async with admin_socket(socket_path, reply_with(reply)):
            client = AdminSocket(socket_path, timeout=0.3)
            if request == "recover":
                await client.recover_account("idm_admin")
            else:
                await client.disable_account("idm_admin")

    for request in ("recover", "disable"):
        with pytest.raises(KanidmProtocolError) as caught:
            run(scenario(request))
        assert str(caught.value).endswith(": unexpected reply")
        assert_no_secret(ALL, caplog, caught.value)


# --- transport failures ---------------------------------------------------------


def test_a_transport_failure_does_not_carry_the_request(
    ca_file: Path, caplog: pytest.LogCaptureFixture
) -> None:
    debug_logging(caplog)

    def handler(request: httpx.Request) -> httpx.Response:
        # The real httpx errors hold the request, with its headers and body.
        raise httpx.ConnectError(f"cannot connect ({LEAK})", request=request)

    for what in (
        lambda c: c.get_group("atlas_owners"),
        lambda c: c.login("idm_admin", SecretStr(PASSWORD)),
    ):
        error = failing(handler, ca_file, what, KanidmUnavailable)
        assert error.__cause__ is None and error.__context__ is None
        assert_no_secret(ALL, caplog, error)


def test_a_server_error_detail_never_holds_the_bearer(
    ca_file: Path, caplog: pytest.LogCaptureFixture
) -> None:
    debug_logging(caplog)
    handler = Recorder(lambda request: httpx.Response(500, text=f"token {BEARER} is odd"))
    error = failing(handler, ca_file, lambda c: c.create_group("atlas_owners"), KanidmServerError)
    assert error.detail == "token <redacted> is odd"
    assert_no_secret([BEARER], caplog, error)


# --- over real TLS (httpx and httpcore are in the path) --------------------------


class _FakeKanidm(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        pass

    def _send(self, status: int, body: object, headers: dict[str, str] | None = None) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:
        step = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["step"]
        session = {"x-kanidm-auth-session-id": SESSION, "set-cookie": COOKIE}
        if "init2" in step:
            self._send(200, {"sessionid": "s", "state": {"choose": ["password"]}}, session)
        elif "begin" in step:
            self._send(200, {"sessionid": "s", "state": {"continue": ["password"]}}, session)
        else:
            self._send(200, {"sessionid": "s", "state": {"success": BEARER}})

    def do_GET(self) -> None:
        if self.headers.get("Authorization") != f"Bearer {BEARER}" or self.headers.get("Cookie"):
            self._send(401, "notauthenticated")
        elif self.path.endswith("/_basic_secret"):
            self._send(200, CLIENT_SECRET)
        else:
            self._send(200, None)


@pytest.fixture
def tls_server(tls_material: TlsMaterial) -> Iterator[str]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeKanidm)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(tls_material.cert_file), str(tls_material.key_file))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # The certificate names `localhost` (and `idp`), not the address.
        yield f"https://localhost:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_over_tls_with_the_right_ca(
    tls_server: str, tls_material: TlsMaterial, caplog: pytest.LogCaptureFixture
) -> None:
    debug_logging(caplog)

    async def scenario() -> list[object]:
        async with KanidmClient(base_url=tls_server, ca_file=tls_material.ca_file) as client:
            await client.login("idm_admin", SecretStr(PASSWORD))
            missing = await client.get_group("nosuch")
            secret = await client.get_oauth2_basic_secret("atlas")
            assert missing is None
            assert secret is not None and secret.get_secret_value() == CLIENT_SECRET
            return [client, secret]

    objects = run(scenario())
    # httpcore really logged at DEBUG, so the check below is not vacuous.
    assert any(r.name.startswith("httpcore") for r in caplog.records)
    assert_no_secret(ALL, caplog, *objects)


def test_over_tls_with_another_ca_is_refused(
    tls_server: str, tls_material: TlsMaterial, caplog: pytest.LogCaptureFixture
) -> None:
    debug_logging(caplog)

    async def scenario() -> None:
        async with KanidmClient(base_url=tls_server, ca_file=tls_material.other_ca_file) as client:
            await client.login("idm_admin", SecretStr(PASSWORD))

    with pytest.raises(KanidmUnavailable) as caught:
        run(scenario())
    assert caught.value.tls is True
    assert "TLS verification" in str(caught.value)
    assert_no_secret(ALL, caplog, caught.value)


def test_over_tls_the_certificate_name_is_checked(
    tls_server: str, tls_material: TlsMaterial
) -> None:
    by_address = tls_server.replace("localhost", "127.0.0.1")

    async def scenario() -> None:
        async with KanidmClient(base_url=by_address, ca_file=tls_material.ca_file) as client:
            await client.get_group("atlas_owners")

    with pytest.raises(KanidmUnavailable) as caught:
        run(scenario())
    assert caught.value.tls is True
