"""Kanidm REST client: the calls the IdP bootstrap needs (PLAN Phase 5b-1; ADR-001).

Behaviour was checked against a real Kanidm 1.12.0 (the image pinned in
deploy/docker-compose.yml):

- Login is three POSTs to /v1/auth (init2, begin, cred). Each step echoes the
  `X-KANIDM-AUTH-SESSION-ID` *response header* of the step before. A refused login is
  a 200 whose state is `denied`, not a 4xx.
- GET on a missing object is `200 null`. A duplicate create is 409. A write on a
  missing object is 404. A missing or bad bearer is 401. A refused action is 403.
- `GET /v1/oauth2/{id}/_basic_secret` is `200 null` both when the client does not
  exist and when the caller may not read it.
- Issuing an API token is not idempotent: a second call with the same label adds a
  second token. List first. Token times (`expiry`, `issued_at`) are Unix seconds.
- Attribute, member, redirect-URL and scope-map writes are idempotent.

Added for the IdP bootstrap (PLAN Phase 5b-2), checked the same way:

- Groups and service accounts are addressed by name or by UUID, also where a group
  is a parameter (scope map, `entry_managed_by`, members). A GET with an unknown
  UUID is `200 null`, like an unknown name. Kanidm answers with SPNs
  (`name@domain`) in `member` and `oauth2_rs_scope_map`, and with a UUID in
  `entry_managed_by`, whichever form the request used.
- An OAuth2 client is addressed by NAME ONLY. Given its UUID, Kanidm answers `GET`
  and `_basic_secret` with `200 null`, `PATCH` and scope-map writes with 404, and
  redirect-URL writes with a silent 200 that changes nothing. So this module
  refuses a UUID there instead of passing it on.
- A redirect URL may have an app's private-use scheme (`app.immich:///oauth-callback`).
  Kanidm also accepts `http://` there, so refusing it is this module's job.
- An OAuth2 client's settings are written with `PATCH /v1/oauth2/{id}` and a body of
  `{"attrs": {name: [value]}}`: 200, and the same body again is 200. An attribute
  Kanidm does not know is 400, a missing client 404. `GET` and `PUT` on
  `/v1/oauth2/{id}/_attr/{attr}` are 405. A `DELETE` there with no body removes the
  whole attribute, whatever it is, so this module has no call for it.
- A new client has neither `oauth2_allow_insecure_client_disable_pkce` nor
  `oauth2_jwt_legacy_crypto_enable`: PKCE is required and legacy crypto is off until
  one of them says `true`.
- `DELETE /v1/service_account/{id}/_api_token/{token_id}` is 200, then 404 for the
  same id. A deleted token that is less than 300 seconds old keeps working until it
  reaches that age; an older one stops at once.
- An API token is a JWS whose payload names its own `token_id`.

Rules this module keeps:

- TLS is always verified against the CA file passed in. There is no way to turn
  verification off.
- Nothing is read from the environment (ADR-004): no proxy variables, no CA
  variables. The URL, CA file and credentials are arguments.
- Credentials are `SecretStr`. Logs hold the method, the path template, the status and
  the duration, never a body or a header. See errors.py for what an error may carry.
- Entries keep only allowlisted attributes (`ENTRY_ATTRIBUTES`); everything else is
  dropped when the response is parsed.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import ssl
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, Self
from urllib.parse import quote, urlsplit

import httpx
from pydantic import SecretStr

from atlas.kernel.identity.kanidm.errors import (
    AuthFailure,
    KanidmAuthError,
    KanidmConflict,
    KanidmError,
    KanidmInvalidRequest,
    KanidmNotFound,
    KanidmPermissionDenied,
    KanidmProtocolError,
    KanidmServerError,
    KanidmUnavailable,
)

logger = logging.getLogger("atlas.identity.kanidm")

EntryKind = Literal["service_account", "group", "oauth2_client"]

# The only attributes an Entry keeps, per object type. Anything else Kanidm returns,
# including attributes this code has never seen, is dropped at parse time. Never add a
# credential here (e.g. oauth2_rs_basic_secret, key_internal_data, primary_credential).
ENTRY_ATTRIBUTES: Mapping[EntryKind, frozenset[str]] = {
    "service_account": frozenset(
        {
            "class",
            "directmemberof",
            "displayname",
            "entry_managed_by",
            "memberof",
            "name",
            "spn",
            "uuid",
        }
    ),
    "group": frozenset(
        {
            "auth_password_minimum_length",
            "authsession_expiry",
            "class",
            "credential_type_minimum",
            "description",
            "entry_managed_by",
            "member",
            "name",
            "privilege_expiry",
            "spn",
            "uuid",
        }
    ),
    "oauth2_client": frozenset(
        {
            "class",
            "displayname",
            "name",
            # "true" lets the client skip PKCE; absent or "false" requires it.
            "oauth2_allow_insecure_client_disable_pkce",
            # "true" signs with RS256 instead of ES256; absent or "false" is ES256.
            "oauth2_jwt_legacy_crypto_enable",
            "oauth2_rs_origin",
            "oauth2_rs_origin_landing",
            "oauth2_rs_scope_map",
            "oauth2_strict_redirect_uri",
            "spn",
            "uuid",
        }
    ),
}

DETAIL_LIMIT = 200
SESSION_HEADER = "X-KANIDM-AUTH-SESSION-ID"

_NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_ATTR = re.compile(r"[a-z][a-z0-9_]{0,63}")
# A private-use URI scheme (RFC 8252 section 7.1): a reversed domain name, so it has a
# dot. That keeps out every registered scheme (http, javascript, data, file, ...).
_APP_REDIRECT = re.compile(r"[a-z][a-z0-9+-]*(?:\.[a-z0-9+-]+)+:[!-~]+")
# One value of oauth2_rs_scope_map, as Kanidm 1.12.0 prints it:
#   atlas_users@id.example.com: {"openid", "profile"}
_SCOPE_MAP = re.compile(r'([^\s:]+): \{((?:"[^"\s]+"(?:, "[^"\s]+")*)?)\}')
_LOGIN_DENIALS: Mapping[str, AuthFailure] = {
    "incorrect password": "incorrect_password",
    "account expired": "account_expired",
}
_UNAUTHENTICATED: Mapping[str, AuthFailure] = {
    "notauthenticated": "not_authenticated",
    "sessionexpired": "session_expired",
}


class _NoResponseHeaders(logging.Filter):
    """Drops httpcore's DEBUG record that prints every response header.

    Kanidm answers the login steps with a session header and a cookie. With the root
    logger at DEBUG, httpcore would write both to the log.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return not str(record.msg).startswith("receive_response_headers")


_no_response_headers = _NoResponseHeaders()
for _httpcore_logger in ("httpcore.http11", "httpcore.http2"):
    logging.getLogger(_httpcore_logger).addFilter(_no_response_headers)


@dataclass(frozen=True, repr=False)
class Entry:
    """A Kanidm object, reduced to the allowlisted attributes of its kind."""

    kind: EntryKind
    attrs: Mapping[str, tuple[str, ...]]

    def first(self, attr: str) -> str | None:
        values = self.attrs.get(attr, ())
        return values[0] if values else None

    @property
    def name(self) -> str | None:
        return self.first("name")

    @property
    def uuid(self) -> str | None:
        return self.first("uuid")

    @property
    def scope_maps(self) -> dict[str, frozenset[str]]:
        """An OAuth2 client's scope maps: the group's SPN (`name@domain`) -> its scopes.

        Kanidm answers with the SPN also when the map was set by the group's UUID.
        A value in another format is a KanidmProtocolError, never skipped: a caller
        that removes unexpected scope maps must see every one of them.
        """
        maps: dict[str, frozenset[str]] = {}
        for value in self.attrs.get("oauth2_rs_scope_map", ()):
            match = _SCOPE_MAP.fullmatch(value)
            if match is None:
                raise KanidmProtocolError(
                    "unexpected scope map format", operation="read oauth2_rs_scope_map"
                )
            maps[match.group(1)] = frozenset(re.findall(r'"([^"]+)"', match.group(2)))
        return maps

    def __repr__(self) -> str:
        return f"Entry(kind={self.kind!r}, name={self.name!r}, attributes={sorted(self.attrs)})"


@dataclass(frozen=True)
class ApiTokenInfo:
    """One API token of a service account. Never the token itself."""

    token_id: str
    label: str
    read_write: bool
    issued_at: datetime
    expiry: datetime | None


def parse_entry(kind: EntryKind, data: object) -> Entry | None:
    """An Entry from Kanidm's `{"attrs": {name: [values]}}`, or None if it is not one."""
    if not isinstance(data, dict):
        return None
    raw = data.get("attrs")
    if not isinstance(raw, dict):
        return None
    allowed = ENTRY_ATTRIBUTES[kind]
    attrs: dict[str, tuple[str, ...]] = {}
    for key, values in raw.items():
        if key not in allowed:
            continue
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            return None
        attrs[key] = tuple(values)
    return Entry(kind=kind, attrs=attrs)


def _name(value: str, what: str = "name") -> str:
    # A UUID that starts with a letter would also fit the pattern. Kanidm never has
    # a name shaped like a UUID, and where only a name works (a new object, an
    # OAuth2 client) it must not be mistaken for one.
    if not isinstance(value, str) or not _NAME.fullmatch(value) or _UUID.fullmatch(value):
        raise ValueError(f"invalid Kanidm {what}: lowercase letters, digits, '_' and '-' only")
    return value


def _id(value: str, what: str = "id") -> str:
    """An existing object's name or its UUID. New objects get a name (`_name`)."""
    if isinstance(value, str) and (_NAME.fullmatch(value) or _UUID.fullmatch(value)):
        return value
    raise ValueError(
        f"invalid Kanidm {what}: a name (lowercase letters, digits, '_' and '-') or a UUID"
    )


def _values(values: Sequence[str], what: str) -> list[str]:
    if isinstance(values, str) or not values:
        raise ValueError(f"{what} must be a non-empty list of strings")
    if not all(isinstance(v, str) and v for v in values):
        raise ValueError(f"{what} must be a non-empty list of strings")
    return list(values)


def _https_url(value: str, what: str) -> str:
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise ValueError(f"{what} must be an https:// URL without credentials")
    return value


def _redirect_uri(value: str) -> str:
    """An https:// URL, or a URI with an app's private-use scheme (RFC 8252 §7.1).

    Kanidm itself would accept http:// here; this is where it is refused.
    """
    if not isinstance(value, str):
        raise ValueError("url must be a string")
    if "#" not in value and _APP_REDIRECT.fullmatch(value):
        return value
    parts = urlsplit(value)
    if parts.scheme == "https" and parts.hostname and not parts.username and not parts.password:
        return value
    raise ValueError(
        "url must be an https:// URL without credentials, or a URI with an app's "
        "private-use scheme (one that contains a dot, e.g. app.immich:///oauth-callback)"
    )


def api_token_id(token: SecretStr) -> str | None:
    """The `token_id` an API token names in its own payload, or None.

    Says which entry of `list_api_tokens` a stored token is. The signature is not
    checked: the answer tells tokens apart, it proves nothing about the token. Never
    raises and never logs, so no part of the token can end up in an error or a log.
    """
    parts = token.get_secret_value().split(".")
    if len(parts) != 3:
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except ValueError:  # also binascii.Error and UnicodeDecodeError
        return None
    token_id = payload.get("token_id") if isinstance(payload, dict) else None
    if isinstance(token_id, str) and _UUID.fullmatch(token_id):
        return token_id
    return None


def _is_tls_failure(exc: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ssl.SSLError):
            return True
        current = current.__cause__ or current.__context__
    return False


def _parse_json(content: bytes) -> tuple[bool, Any]:
    # Returns a flag instead of raising: json's own error object holds the whole
    # document, and an error raised while handling it would carry it as its context.
    try:
        return True, json.loads(content)
    except ValueError:
        return False, None


def _parse_time(value: object) -> datetime | None:
    # Kanidm 1.12.0 writes API-token times as Unix seconds.
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    try:
        return datetime.fromtimestamp(value, UTC)
    except (OverflowError, OSError, ValueError):
        return None


class KanidmClient:
    """One connection pool to one Kanidm, authenticated by `login()` or by `token`.

    Use it as an async context manager, or call `aclose()`.

    `base_url` is Kanidm's internal URL (e.g. ``https://idp:8443``) and `ca_file` the
    internal CA certificate that signed its certificate. `transport` replaces the
    network for unit tests and is not meant for anything else.
    """

    def __init__(
        self,
        *,
        base_url: str,
        ca_file: Path,
        token: SecretStr | None = None,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        _https_url(base_url, "base_url")
        parts = urlsplit(base_url)
        if parts.path not in ("", "/") or parts.query or parts.fragment:
            raise ValueError("base_url must not have a path, query or fragment")
        # Always built, also with a test transport: a missing or unreadable CA file
        # fails here, before any request.
        context = ssl.create_default_context(cafile=str(ca_file))
        self._token = token
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            verify=context,
            timeout=httpx.Timeout(timeout, connect=min(timeout, 5.0)),
            transport=transport,
            trust_env=False,
            follow_redirects=False,
        )

    def __repr__(self) -> str:
        state = "authenticated" if self._token is not None else "anonymous"
        return f"KanidmClient({str(self._http.base_url)!r}, {state})"

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    # --- request core ------------------------------------------------------------

    async def _call(
        self,
        method: str,
        template: str,
        *,
        path: Mapping[str, str] | None = None,
        body: object = None,
        sensitive: bool = False,
        authenticated: bool = True,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[Any, httpx.Headers]:
        """One request. Returns the decoded JSON body of a 200 and the response headers.

        `template` is what logs and errors show; `path` fills its placeholders.
        `sensitive` marks an endpoint whose response can hold a credential: its errors
        then carry nothing from the response.
        """
        operation = f"{method} {template}"
        url = template.format(**{k: quote(v, safe="") for k, v in (path or {}).items()})
        send_headers = {"Accept": "application/json", **(headers or {})}
        if authenticated and self._token is not None:
            send_headers["Authorization"] = f"Bearer {self._token.get_secret_value()}"
        started = time.monotonic()
        response: httpx.Response | None = None
        failure: tuple[str, bool] | None = None
        try:
            response = await self._http.request(
                method, url, json=body if body is not None else None, headers=send_headers
            )
        except httpx.TimeoutException:
            failure = ("Kanidm did not answer in time", False)
        except httpx.RequestError as exc:
            tls = _is_tls_failure(exc)
            what = "TLS verification against the CA file failed" if tls else "connection failed"
            failure = (f"{what} ({type(exc).__name__})", tls)
        finally:
            # Kanidm sets a cookie during login; this client authenticates by bearer only.
            self._http.cookies.clear()
        # Raised here, outside the except block, so the error has no __context__: the
        # httpx exception holds the request, and with it the Authorization header.
        if failure is not None or response is None:
            description, tls = failure or ("no response", False)
            logger.warning("%s failed: %s", operation, description)
            raise KanidmUnavailable(description, operation=operation, tls=tls)

        elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.info("%s -> %d (%d ms)", operation, response.status_code, elapsed_ms)
        if response.status_code != 200:
            raise self._error(operation, response, sensitive=sensitive)
        ok, data = _parse_json(response.content)
        if not ok:
            raise KanidmProtocolError(
                "the response is not JSON", operation=operation, status=response.status_code
            )
        return data, response.headers

    def _detail(self, response: httpx.Response) -> str | None:
        text = " ".join(response.text.split())
        if self._token is not None:
            text = text.replace(self._token.get_secret_value(), "<redacted>")
        return text[:DETAIL_LIMIT] or None

    def _error(self, operation: str, response: httpx.Response, *, sensitive: bool) -> KanidmError:
        status = response.status_code
        detail = None if sensitive else self._detail(response)
        ok, data = _parse_json(response.content)
        if status == 401:
            reason = (
                _UNAUTHENTICATED.get(data, "not_authenticated") if isinstance(data, str) else None
            )
            return KanidmAuthError(
                "not authenticated",
                operation=operation,
                status=status,
                reason=reason or "not_authenticated",
            )
        if status == 403:
            return KanidmPermissionDenied("permission denied", operation=operation, status=status)
        if status == 404:
            if ok:
                return KanidmNotFound(
                    "no such object", operation=operation, status=status, detail=detail
                )
            return KanidmProtocolError("no such route", operation=operation, status=status)
        if status == 409:
            names: tuple[str, ...] = ()
            if not sensitive and isinstance(data, dict):
                raw = data.get("conflicting_attributes")
                if isinstance(raw, list):
                    names = tuple(n for n in raw if isinstance(n, str) and _ATTR.fullmatch(n))
            return KanidmConflict(
                "already exists",
                operation=operation,
                status=status,
                detail=detail,
                conflicting_attributes=names,
            )
        if status in (400, 422):
            return KanidmInvalidRequest(
                "Kanidm rejected the request", operation=operation, status=status, detail=detail
            )
        if status >= 500:
            return KanidmServerError(
                "Kanidm reported an error", operation=operation, status=status, detail=detail
            )
        return KanidmProtocolError("unexpected status", operation=operation, status=status)

    async def _write(
        self, method: str, template: str, *, path: Mapping[str, str], body: object
    ) -> None:
        await self._call(method, template, path=path, body=body)

    async def _get_entry(self, kind: EntryKind, template: str, name: str) -> Entry | None:
        # An OAuth2 client's entry includes its secret (dropped by the allowlist), so
        # nothing from that response may reach an error either.
        sensitive = kind == "oauth2_client"
        # Kanidm resolves an OAuth2 client by name only (see the module docstring).
        ident = _name(name) if kind == "oauth2_client" else _id(name)
        data, _ = await self._call("GET", template, path={"id": ident}, sensitive=sensitive)
        if data is None:
            return None
        entry = parse_entry(kind, data)
        if entry is None:
            raise KanidmProtocolError(
                "the response is not an entry", operation=f"GET {template}", status=200
            )
        return entry

    # --- login -------------------------------------------------------------------

    async def login(self, username: str, password: SecretStr) -> None:
        """Log in with a password and keep the bearer for the following calls.

        For the built-in service accounts (`idm_admin`), whose password comes from
        the admin socket. Raises KanidmAuthError when Kanidm refuses the login.
        """
        _name(username, "username")
        operation = "POST /v1/auth"

        def protocol_error(description: str) -> KanidmProtocolError:
            return KanidmProtocolError(description, operation=operation, status=200)

        async def step(body: object, session: str | None) -> tuple[dict[str, Any], str | None]:
            headers = {SESSION_HEADER: session} if session is not None else None
            data, response_headers = await self._call(
                "POST", "/v1/auth", body=body, sensitive=True, authenticated=False, headers=headers
            )
            if not isinstance(data, dict) or not set(data) <= {"sessionid", "state"}:
                raise protocol_error("unexpected login response")
            state = data.get("state")
            if not isinstance(state, dict) or len(state) != 1:
                raise protocol_error("unexpected login response")
            if "denied" in state:
                denial = state["denied"]
                reason = (
                    _LOGIN_DENIALS.get(denial, "denied") if isinstance(denial, str) else "denied"
                )
                raise KanidmAuthError("login refused", operation=operation, reason=reason)
            return state, response_headers.get(SESSION_HEADER)

        def offers_password(state: Mapping[str, Any], key: str) -> bool:
            options = state.get(key)
            return isinstance(options, list) and "password" in options

        init = {"init2": {"username": username, "issue": "token", "privileged": True}}
        state, session = await step({"step": init}, None)
        if not offers_password(state, "choose"):
            raise protocol_error("Kanidm does not offer a password login for this account")
        if session is None:
            raise protocol_error("the login response has no session header")

        state, next_session = await step({"step": {"begin": "password"}}, session)
        if not offers_password(state, "continue"):
            raise protocol_error("unexpected login response")

        cred = {"cred": {"password": password.get_secret_value()}}
        state, _ = await step({"step": cred}, next_session or session)
        token = state.get("success")
        if not isinstance(token, str) or not token:
            raise protocol_error("unexpected login response")
        self._token = SecretStr(token)

    # --- service accounts --------------------------------------------------------

    async def get_service_account(self, name: str) -> Entry | None:
        return await self._get_entry("service_account", "/v1/service_account/{id}", name)

    async def create_service_account(
        self, name: str, *, display_name: str, managed_by: str
    ) -> None:
        """Create a service account managed by the group `managed_by` (409 if it exists).

        `managed_by` is the group's name or UUID. Kanidm answers with its UUID.
        """
        attrs = {
            "name": [_name(name)],
            "displayname": [display_name],
            "entry_managed_by": [_id(managed_by, "managed_by")],
        }
        await self._write("POST", "/v1/service_account", path={}, body={"attrs": attrs})

    async def list_api_tokens(self, account: str) -> list[ApiTokenInfo]:
        template = "/v1/service_account/{id}/_api_token"
        data, _ = await self._call("GET", template, path={"id": _id(account, "account")})
        tokens: list[ApiTokenInfo] = []
        items = data if isinstance(data, list) else None
        for item in items or []:
            if not isinstance(item, dict):
                items = None
                break
            token_id, label = item.get("token_id"), item.get("label")
            purpose, issued_at = item.get("purpose"), _parse_time(item.get("issued_at"))
            expiry_raw = item.get("expiry")
            expiry = _parse_time(expiry_raw)
            if (
                not isinstance(token_id, str)
                or not isinstance(label, str)
                or not isinstance(purpose, str)
                or issued_at is None
                or (expiry_raw is not None and expiry is None)
            ):
                items = None
                break
            tokens.append(
                ApiTokenInfo(
                    token_id=token_id,
                    label=label,
                    read_write=purpose == "readwrite",
                    issued_at=issued_at,
                    expiry=expiry,
                )
            )
        if items is None:
            raise KanidmProtocolError(
                "the response is not a token list", operation=f"GET {template}", status=200
            )
        return tokens

    async def issue_api_token(
        self, account: str, *, label: str, read_write: bool, expiry: datetime | None = None
    ) -> SecretStr:
        """Issue a new API token and return it. Kanidm shows a token only once.

        Not idempotent: every call adds a token, also with a label that exists. Call
        `list_api_tokens` first.
        """
        if expiry is not None and expiry.tzinfo is None:
            raise ValueError("expiry must be timezone-aware")
        if not label:
            raise ValueError("label must not be empty")
        template = "/v1/service_account/{id}/_api_token"
        body = {
            "label": label,
            # Unix seconds, like the times Kanidm returns in the token list.
            "expiry": None if expiry is None else int(expiry.timestamp()),
            "read_write": read_write,
            "compact": False,
        }
        data, _ = await self._call(
            "POST", template, path={"id": _id(account, "account")}, body=body, sensitive=True
        )
        if not isinstance(data, str) or not data:
            raise KanidmProtocolError(
                "the response is not a token", operation=f"POST {template}", status=200
            )
        return SecretStr(data)

    async def delete_api_token(self, account: str, token_id: str) -> bool:
        """Delete one API token. True if it was deleted, False if Kanidm has no such
        token (404): deleting twice is safe.

        `token_id` comes from `list_api_tokens` or `api_token_id`. Kanidm keeps
        accepting a deleted token until the token is 300 seconds old, so one issued a
        moment ago is not refused at once. A missing account is also a 404, so False
        does not prove the account exists.
        """
        if not isinstance(token_id, str) or not _UUID.fullmatch(token_id):
            raise ValueError("token_id must be a UUID")
        template = "/v1/service_account/{id}/_api_token/{token_id}"
        path = {"id": _id(account, "account"), "token_id": token_id}
        missing = False
        try:
            await self._call("DELETE", template, path=path)
        except KanidmNotFound:
            missing = True
        return not missing

    # --- groups ------------------------------------------------------------------

    async def get_group(self, name: str) -> Entry | None:
        return await self._get_entry("group", "/v1/group/{id}", name)

    async def create_group(self, name: str) -> None:
        """Create a group (409 if it exists)."""
        await self._write("POST", "/v1/group", path={}, body={"attrs": {"name": [_name(name)]}})

    async def add_group_attr(self, group: str, attr: str, values: Sequence[str]) -> None:
        """Add values to an attribute (e.g. class `account_policy`). Idempotent."""
        await self._group_attr("POST", group, attr, values)

    async def set_group_attr(self, group: str, attr: str, values: Sequence[str]) -> None:
        """Replace an attribute's values (e.g. `credential_type_minimum`). Idempotent."""
        await self._group_attr("PUT", group, attr, values)

    async def add_group_members(self, group: str, members: Sequence[str]) -> None:
        """Add members by name. Idempotent. A member that does not exist is a 500
        from Kanidm 1.12.0 (KanidmServerError), not a 404."""
        await self._group_attr("POST", group, "member", members)

    async def _group_attr(self, method: str, group: str, attr: str, values: Sequence[str]) -> None:
        if not _ATTR.fullmatch(attr):
            raise ValueError("invalid Kanidm attribute name")
        await self._write(
            method,
            "/v1/group/{id}/_attr/{attr}",
            path={"id": _id(group, "group"), "attr": attr},
            body=_values(values, attr),
        )

    # --- OAuth2 clients ----------------------------------------------------------

    async def get_oauth2_client(self, name: str) -> Entry | None:
        return await self._get_entry("oauth2_client", "/v1/oauth2/{id}", name)

    async def create_oauth2_basic_client(
        self, name: str, *, display_name: str, landing_url: str, strict_redirect_uri: bool = True
    ) -> None:
        """Create a confidential ("basic") OAuth2 client (409 if it exists)."""
        attrs = {
            "name": [_name(name)],
            "displayname": [display_name],
            "oauth2_rs_origin_landing": [_https_url(landing_url, "landing_url")],
            "oauth2_strict_redirect_uri": ["true" if strict_redirect_uri else "false"],
        }
        await self._write("POST", "/v1/oauth2/_basic", path={}, body={"attrs": attrs})

    async def update_oauth2_client(
        self,
        name: str,
        *,
        landing_url: str | None = None,
        strict_redirect_uri: bool | None = None,
        legacy_crypto: bool | None = None,
        require_pkce: bool | None = None,
    ) -> None:
        """Set the given settings of an OAuth2 client in one request. Idempotent.

        An argument left at None is not touched. These four are the only attributes
        this client writes this way:

        - `landing_url`: the https:// URL Kanidm sends users to, and a redirect URL.
        - `strict_redirect_uri`: redirect URLs must match exactly.
        - `legacy_crypto`: sign with RS256 instead of ES256.
        - `require_pkce`: False lets the client skip PKCE. (The attribute Kanidm
          stores is the opposite, `oauth2_allow_insecure_client_disable_pkce`.)

        A missing client is KanidmNotFound.
        """
        attrs: dict[str, list[str]] = {}
        if landing_url is not None:
            attrs["oauth2_rs_origin_landing"] = [_https_url(landing_url, "landing_url")]
        flags = (
            ("oauth2_strict_redirect_uri", strict_redirect_uri, False),
            ("oauth2_jwt_legacy_crypto_enable", legacy_crypto, False),
            ("oauth2_allow_insecure_client_disable_pkce", require_pkce, True),
        )
        for attr, value, inverted in flags:
            if value is None:
                continue
            if not isinstance(value, bool):
                raise ValueError(f"{attr} takes True or False")
            attrs[attr] = ["true" if value != inverted else "false"]
        if not attrs:
            raise ValueError("update_oauth2_client needs at least one setting")
        await self._write(
            "PATCH", "/v1/oauth2/{id}", path={"id": _name(name)}, body={"attrs": attrs}
        )

    async def add_oauth2_redirect_url(self, name: str, url: str) -> None:
        """Allow one more redirect URL. Idempotent.

        `url` is an https:// URL, or a URI with an app's private-use scheme such as
        `app.immich:///oauth-callback`. Anything else, http:// included, is a ValueError.
        """
        await self._redirect_url("POST", name, url)

    async def remove_oauth2_redirect_url(self, name: str, url: str) -> None:
        """Remove a redirect URL. Idempotent: removing one that is not set succeeds."""
        await self._redirect_url("DELETE", name, url)

    async def _redirect_url(self, method: str, name: str, url: str) -> None:
        # Always with the one URL as the body: a DELETE without a body would remove
        # the whole attribute.
        await self._write(
            method,
            "/v1/oauth2/{id}/_attr/oauth2_rs_origin",
            path={"id": _name(name)},
            body=[_redirect_uri(url)],
        )

    async def set_oauth2_scope_map(self, name: str, group: str, scopes: Sequence[str]) -> None:
        """Set the scopes members of `group` get from this client. Idempotent.

        `name` is the client's name. `group` is the group's name or UUID.
        `Entry.scope_maps` reads the result back.
        """
        await self._write(
            "POST",
            "/v1/oauth2/{id}/_scopemap/{group}",
            path={"id": _name(name), "group": _id(group, "group")},
            body=_values(scopes, "scopes"),
        )

    async def get_oauth2_basic_secret(self, name: str) -> SecretStr | None:
        """The client secret, or None.

        None means the client is missing OR the caller may not read its secret:
        Kanidm answers `200 null` for both. Callers must treat None as a failure.
        """
        template = "/v1/oauth2/{id}/_basic_secret"
        data, _ = await self._call("GET", template, path={"id": _name(name)}, sensitive=True)
        if data is None:
            return None
        if not isinstance(data, str) or not data:
            raise KanidmProtocolError(
                "the response is not a secret", operation=f"GET {template}", status=200
            )
        return SecretStr(data)
