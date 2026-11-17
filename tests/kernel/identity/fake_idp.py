"""A stateful in-memory stand-in for the bundled Kanidm, for the bootstrap's unit tests.

It answers the calls of `atlas.kernel.identity.bootstrap.IdpClient` the way Kanidm 1.12.0
does where the bootstrap relies on it: names or UUIDs for groups, SPNs (`name@domain`) in
`member` and in scope maps, a UUID in `entry_managed_by`, no client secret without a
client, a deleted API token that keeps working for a while, a 500 for a member that does
not exist. Every call is recorded in `trace`, shared with the fake admin socket, so tests
can assert the order of calls across both. `fail_on` makes a call raise.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime

from pydantic import SecretStr

from atlas.kernel.identity.kanidm import (
    ApiTokenInfo,
    Entry,
    KanidmAdminSocketError,
    KanidmAuthError,
    KanidmConflict,
    KanidmNotFound,
    KanidmServerError,
)

DOMAIN = "id.test.invalid"
_NAMESPACE = uuid.UUID("6f1a3c52-8e0d-4a57-9d3b-2c4e7a91b0f8")


def stable_uuid(name: str) -> str:
    return str(uuid.uuid5(_NAMESPACE, name))


def derived(tag: str, length: int = 40) -> str:
    """A made-up credential: unique per tag and not a literal in this file."""
    return (hashlib.sha256(tag.encode()).hexdigest() * 3)[:length]


def make_jws(token_id: str, nonce: str) -> str:
    def part(data: object) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    return ".".join(
        [part({"alg": "ES256"}), part({"token_id": token_id, "n": nonce}), derived(nonce, 16)]
    )


class Trace(list[str]):
    """The calls made, in order: ``"idp.get_group:atlas_users"``, ``"socket.disable"``."""

    def count_prefix(self, prefix: str) -> int:
        return sum(1 for item in self if item.startswith(prefix))


class FakeIdp:
    def __init__(self, trace: Trace | None = None) -> None:
        self.trace = trace if trace is not None else Trace()
        self.fail_on: dict[str, BaseException] = {}
        self.groups: dict[str, dict[str, object]] = {}
        self.accounts: dict[str, dict[str, object]] = {}
        self.tokens: dict[str, dict[str, object]] = {}  # token_id -> {label, nonce}
        self.in_grace: set[str] = set()  # deleted, but still accepted (Kanidm: under 300 s old)
        self.issued = 0
        self.clients: dict[str, dict[str, object]] = {}
        self.client_secrets: dict[str, str] = {}
        self.no_secret = False  # Kanidm's `200 null` for a secret the caller may not read
        self.admin_password: str | None = None
        self.admin_enabled = True
        self.logged_in = False
        for name in ("idm_admins", "idm_people_admins", "idm_group_admins"):
            self._new_group(name)
        self._new_group("idm_all_persons")
        self.groups["idm_all_persons"].update(
            {"class": {"account_policy", "group", "object"}, "credential_type_minimum": "mfa"}
        )

    # --- helpers -----------------------------------------------------------------

    async def _enter(self, method: str, *args: object, admin: bool = True) -> None:
        first = str(args[0]) if args else ""
        self.trace.append(f"idp.{method}:{first}" if first else f"idp.{method}")
        for key in (f"{method}:{first}", method):
            if key in self.fail_on:
                raise self.fail_on.pop(key)
        if admin and method != "login" and not self.logged_in:
            # Disabling the account ends its session at once (probe P6).
            raise KanidmAuthError(
                "not authenticated", operation=method, status=401, reason="session_expired"
            )

    def _new_group(self, name: str) -> dict[str, object]:
        group: dict[str, object] = {
            "uuid": stable_uuid(f"group-{name}"),
            "class": {"group", "object"},
            "member": set(),
        }
        self.groups[name] = group
        return group

    def _find_group(self, ident: str) -> tuple[str, dict[str, object]] | None:
        for name, group in self.groups.items():
            if ident in (name, group["uuid"]):
                return name, group
        return None

    def _spn(self, name: str) -> str:
        return f"{name}@{DOMAIN}"

    def _group_entry(self, name: str) -> Entry:
        group = self.groups[name]
        attrs: dict[str, tuple[str, ...]] = {
            "name": (name,),
            "spn": (self._spn(name),),
            "uuid": (str(group["uuid"]),),
            "class": tuple(sorted(group["class"])),  # type: ignore[call-overload]
        }
        if group["member"]:
            attrs["member"] = tuple(sorted(group["member"]))  # type: ignore[call-overload]
        for attr in ("credential_type_minimum", "authsession_expiry"):
            if attr in group:
                attrs[attr] = (str(group[attr]),)
        return Entry(kind="group", attrs=attrs)

    # --- login -------------------------------------------------------------------

    async def login(self, username: str, password: SecretStr) -> None:
        await self._enter("login", username)
        if not self.admin_enabled:
            raise KanidmAuthError(
                "login refused", operation="POST /v1/auth", reason="account_expired"
            )
        if password.get_secret_value() != self.admin_password:
            raise KanidmAuthError(
                "login refused", operation="POST /v1/auth", reason="incorrect_password"
            )
        self.logged_in = True

    # --- groups ------------------------------------------------------------------

    async def get_group(self, name: str, *, admin: bool = True) -> Entry | None:
        await self._enter("get_group", name, admin=admin)
        found = self._find_group(name)
        return None if found is None else self._group_entry(found[0])

    async def create_group(self, name: str) -> None:
        await self._enter("create_group", name)
        if name in self.groups:
            raise KanidmConflict("already exists", operation="POST /v1/group", status=409)
        self._new_group(name)

    def _group_for_write(self, ident: str, operation: str) -> tuple[str, dict[str, object]]:
        found = self._find_group(ident)
        if found is None:
            raise KanidmNotFound("no such object", operation=operation, status=404)
        return found

    async def add_group_attr(self, group: str, attr: str, values: Sequence[str]) -> None:
        await self._enter("add_group_attr", group, attr)
        _, record = self._group_for_write(group, "POST /v1/group/{id}/_attr/{attr}")
        assert attr == "class"
        record["class"] = set(record["class"]) | set(values)  # type: ignore[call-overload]

    async def set_group_attr(self, group: str, attr: str, values: Sequence[str]) -> None:
        await self._enter("set_group_attr", group, attr)
        _, record = self._group_for_write(group, "PUT /v1/group/{id}/_attr/{attr}")
        assert attr in ("credential_type_minimum", "authsession_expiry")
        record[attr] = values[0]

    async def add_group_members(self, group: str, members: Sequence[str]) -> None:
        await self._enter("add_group_members", group)
        _, record = self._group_for_write(group, "POST /v1/group/{id}/_attr/member")
        for member in members:
            if member not in self.groups and member not in self.accounts:
                raise KanidmServerError("Kanidm reported an error", operation="POST", status=500)
            record["member"] = set(record["member"]) | {self._spn(member)}  # type: ignore[call-overload]

    # --- service accounts and tokens ----------------------------------------------

    async def get_service_account(self, name: str) -> Entry | None:
        await self._enter("get_service_account", name)
        account = self.accounts.get(name)
        if account is None:
            return None
        return Entry(
            kind="service_account",
            attrs={
                "name": (name,),
                "uuid": (str(account["uuid"]),),
                "entry_managed_by": (str(account["managed_by"]),),
            },
        )

    async def create_service_account(
        self, name: str, *, display_name: str, managed_by: str
    ) -> None:
        await self._enter("create_service_account", name)
        if name in self.accounts:
            raise KanidmConflict("already exists", operation="POST", status=409)
        found = self._find_group(managed_by)
        assert found is not None
        self.accounts[name] = {
            "uuid": stable_uuid(f"account-{name}"),
            "managed_by": found[1]["uuid"],
        }

    async def list_api_tokens(self, account: str) -> list[ApiTokenInfo]:
        await self._enter("list_api_tokens", account)
        return [
            ApiTokenInfo(
                token_id=token_id,
                label=str(info["label"]),
                read_write=True,
                issued_at=datetime(2026, 11, 17, tzinfo=UTC),
                expiry=None,
            )
            for token_id, info in self.tokens.items()
        ]

    async def issue_api_token(self, account: str, *, label: str, read_write: bool) -> SecretStr:
        await self._enter("issue_api_token", account)
        self.issued += 1
        nonce = f"token-{self.issued}-{label}"
        token_id = stable_uuid(nonce)
        self.tokens[token_id] = {"label": label, "nonce": nonce}
        return SecretStr(make_jws(token_id, nonce))

    def issue_token_by_hand(self, label: str, nonce: str) -> SecretStr:
        """A token created outside the bootstrap, e.g. left by an earlier run."""
        token_id = stable_uuid(nonce)
        self.tokens[token_id] = {"label": label, "nonce": nonce}
        return SecretStr(make_jws(token_id, nonce))

    async def delete_api_token(self, account: str, token_id: str) -> bool:
        await self._enter("delete_api_token", account)
        if token_id not in self.tokens:
            return False
        del self.tokens[token_id]
        # Kanidm 1.12.0: a token under 300 seconds old keeps working after its deletion.
        self.in_grace.add(token_id)
        return True

    def token_is_accepted(self, token: SecretStr) -> bool:
        payload = token.get_secret_value().split(".")[1]
        token_id = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))[
            "token_id"
        ]
        return token_id in self.tokens or token_id in self.in_grace

    def expire_grace(self) -> None:
        self.in_grace.clear()

    # --- OAuth2 clients ----------------------------------------------------------

    def _client_entry(self, name: str) -> Entry:
        client = self.clients[name]
        attrs: dict[str, tuple[str, ...]] = {
            "name": (name,),
            "oauth2_rs_origin_landing": (str(client["landing"]),),
            "oauth2_rs_origin": tuple(sorted(client["origins"])),  # type: ignore[call-overload]
            "oauth2_strict_redirect_uri": (str(client["strict"]).lower(),),
        }
        if client.get("legacy") is not None:
            attrs["oauth2_jwt_legacy_crypto_enable"] = (str(client["legacy"]).lower(),)
        if client.get("no_pkce") is not None:
            attrs["oauth2_allow_insecure_client_disable_pkce"] = (str(client["no_pkce"]).lower(),)
        maps = client["scope_maps"]
        if maps:
            attrs["oauth2_rs_scope_map"] = tuple(
                f"{self._spn(group)}: {{{', '.join(json.dumps(s) for s in sorted(scopes))}}}"
                for group, scopes in sorted(maps.items())  # type: ignore[attr-defined]
            )
        return Entry(kind="oauth2_client", attrs=attrs)

    async def get_oauth2_client(self, name: str) -> Entry | None:
        await self._enter("get_oauth2_client", name)
        return self._client_entry(name) if name in self.clients else None

    async def create_oauth2_basic_client(
        self, name: str, *, display_name: str, landing_url: str, strict_redirect_uri: bool = True
    ) -> None:
        await self._enter("create_oauth2_basic_client", name)
        if name in self.clients:
            raise KanidmConflict("already exists", operation="POST", status=409)
        self.clients[name] = {
            "landing": landing_url,
            "origins": {landing_url},
            "strict": strict_redirect_uri,
            "scope_maps": {},
        }
        self.client_secrets[name] = derived(f"client-{name}", 48)

    def _client(self, name: str) -> dict[str, object]:
        if name not in self.clients:
            raise KanidmNotFound("no such object", operation="PATCH /v1/oauth2/{id}", status=404)
        return self.clients[name]

    async def update_oauth2_client(
        self,
        name: str,
        *,
        landing_url: str | None = None,
        strict_redirect_uri: bool | None = None,
        legacy_crypto: bool | None = None,
        require_pkce: bool | None = None,
    ) -> None:
        await self._enter("update_oauth2_client", name)
        client = self._client(name)
        if landing_url is not None:
            client["landing"] = landing_url
            client["origins"] = set(client["origins"]) | {landing_url}  # type: ignore[call-overload]
        if strict_redirect_uri is not None:
            client["strict"] = strict_redirect_uri
        if legacy_crypto is not None:
            client["legacy"] = legacy_crypto
        if require_pkce is not None:
            client["no_pkce"] = not require_pkce

    async def add_oauth2_redirect_url(self, name: str, url: str) -> None:
        await self._enter("add_oauth2_redirect_url", name)
        client = self._client(name)
        client["origins"] = set(client["origins"]) | {url}  # type: ignore[call-overload]

    async def remove_unwanted_oauth2_redirect_url(self, name: str, url: str) -> None:
        await self._enter("remove_unwanted_oauth2_redirect_url", name)
        client = self._client(name)
        client["origins"] = set(client["origins"]) - {url}  # type: ignore[call-overload]

    async def set_oauth2_scope_map(self, name: str, group: str, scopes: Sequence[str]) -> None:
        await self._enter("set_oauth2_scope_map", name)
        client = self._client(name)
        found = self._find_group(group)
        if found is None:
            raise KanidmNotFound("no such object", operation="POST", status=404)
        client["scope_maps"][found[0]] = frozenset(scopes)  # type: ignore[index]

    async def remove_oauth2_scope_map(self, name: str, group: str) -> None:
        await self._enter("remove_oauth2_scope_map", name)
        client = self._client(name)
        if self._find_group(group) is None:
            raise KanidmNotFound("no such object", operation="DELETE", status=404)
        client["scope_maps"].pop(group, None)  # type: ignore[attr-defined]

    async def get_oauth2_basic_secret(self, name: str) -> SecretStr | None:
        await self._enter("get_oauth2_basic_secret", name)
        if self.no_secret or name not in self.clients:
            return None
        return SecretStr(self.client_secrets[name])


class FakeSocket:
    """Kanidm's admin socket: RecoverAccount sets a password and enables the account."""

    def __init__(self, idp: FakeIdp) -> None:
        self.idp = idp
        self.fail_on: dict[str, BaseException] = {}

    async def recover_account(self, name: str) -> SecretStr:
        self.idp.trace.append("socket.recover")
        if "recover" in self.fail_on:
            raise self.fail_on.pop("recover")
        self.idp.admin_password = derived(
            f"admin-password-{self.idp.trace.count_prefix('socket.recover')}"
        )
        self.idp.admin_enabled = True
        return SecretStr(self.idp.admin_password)

    async def disable_account(self, name: str) -> None:
        self.idp.trace.append("socket.disable")
        if "disable" in self.fail_on:
            raise self.fail_on.pop("disable")
        self.idp.admin_enabled = False
        self.idp.logged_in = False


def socket_error() -> KanidmAdminSocketError:
    return KanidmAdminSocketError("Kanidm refused the request", operation="admin socket X")


class FakeSession:
    """A connection with a bearer token: it can only read groups, and only with a valid token."""

    def __init__(self, idp: FakeIdp, token: SecretStr) -> None:
        self._idp = idp
        self._token = token

    async def get_group(self, name: str) -> Entry | None:
        if not self._idp.token_is_accepted(self._token):
            raise KanidmAuthError(
                "not authenticated", operation="GET", status=401, reason="not_authenticated"
            )
        return await self._idp.get_group(name, admin=False)


def make_connect(idp: FakeIdp):  # type: ignore[no-untyped-def]
    """The bootstrap's `ClientFactory`: no token is the admin session, a token is a reader."""

    @contextlib.asynccontextmanager
    async def connect(token: SecretStr | None) -> AsyncIterator[object]:
        if token is None:
            yield idp
        else:
            yield FakeSession(idp, token)

    return connect
