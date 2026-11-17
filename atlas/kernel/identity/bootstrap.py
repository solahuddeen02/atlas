"""The bundled IdP's bootstrap: everything Atlas needs inside Kanidm (ADR-001, PLAN 5b-2).

`atlas-admin idp-bootstrap` runs this once per start of the stack, after Kanidm is
healthy and before atlas-api. It is idempotent: each step reads first and writes only
on a difference, and says what it did (created, kept or updated) in one line that holds
names and UUIDs and nothing secret.

Full run, in order:

 1. settings; the CA file and the admin socket must exist (the CLI checks);
 2. `complete: false` into bootstrap.json: the first act of EVERY run, the fast path
    included (atlas.kernel.identity.record says why);
 3. the fast path (below), else a full run:
 4. RecoverAccount for `idm_admin` on the admin socket, then a password login. From
    here on `DisableAccount` runs on every way out: success, error, SIGTERM;
 5. the groups: `atlas_users`, the five role groups with their credential policies,
    every role group a member of `atlas_users`, and `immich_users` with Immich;
 6. `idm_all_persons` to credential type `any`, only after step 5 has been read back;
 7. the service account `atlas-console` (managed by `idm_admins`) and its membership of
    `idm_people_admins`; then the least-privilege change (below);
 8. its API token: exactly one with our label, written to idp.env before older ones
    are deleted;
 9. the OAuth2 client `atlas`: landing URL = the OIDC callback and its only redirect
    URL, strict redirect matching, PKCE required, legacy crypto off, the scope map for
    `atlas_users`; its secret is read from Kanidm (no secret is a failure);
10. the same for `immich` (with Immich only): three redirect URLs, its scope map for
    `immich_users`, its secret into clients/immich.env;
11. groups.json and idp.env, written only if they differ. Kanidm's value wins over a
    stored one;
12. DisableAccount for `idm_admin`;
13. `complete: true` into bootstrap.json: the last act, after everything else worked.

Least privilege (step 7, version 2): the console must change members of the five role
groups and `immich_users` and of no other group. `idm_group_admins` would let it edit
every group that is not high-privilege (it could even delete `atlas_users`), so the
console is not a member of it. Instead each of the six groups is managed by the console
(`entry_managed_by`). The order matters and is checked: set the manager on all six, read
every one back, and only then remove the console from `idm_group_admins`. A failure in
between fails the run (the record stays incomplete) and the next run resumes: every write
is made only on a difference. `--verify` also removes members of the six groups that are
not persons (log: group, name and UUID).

The fast path skips 4 to 11 when everything below holds, and still sends
DisableAccount (no recover, no login, and a failure of that call fails the run), then
does 13: the last run completed, the recorded version and digest match, the stored
token works and every group UUID matches Kanidm.
It cannot see drift in clients or policies; `verify=True` forces a full run, which also
removes scope maps this module did not set (on `atlas` and `immich` only).

Secrets: the recovered password and the session bearer live in memory only. The files
this writes are idp.env (the client secret and the console token), groups.json and
bootstrap.json (not secret) under `state_dir`, and Immich's client secret under
`clients_dir`. No line printed or raised here carries a secret.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from dotenv import dotenv_values
from pydantic import SecretStr

from atlas.kernel.identity.kanidm import (
    ApiTokenInfo,
    Entry,
    KanidmAuthError,
    KanidmError,
    KanidmPermissionDenied,
    api_token_id,
)
from atlas.kernel.identity.record import (
    RECORD_FILE,
    is_current_version,
    read_record,
    record_text,
)
from atlas.kernel.identity.secret_files import (
    read_text,
    write_atomic,
    write_if_different,
)

ADMIN_ACCOUNT = "idm_admin"
USERS_GROUP = "atlas_users"
IMMICH_GROUP = "immich_users"
CONSOLE_ACCOUNT = "atlas-console"
CONSOLE_TOKEN_LABEL = "atlas-console"
CONSOLE_MANAGER_GROUP = "idm_admins"
CONSOLE_GROUPS = ("idm_people_admins",)
# Held by a console from before version 2; removed once the six groups are managed.
FORMER_CONSOLE_GROUPS = ("idm_group_admins",)
ALL_PERSONS_GROUP = "idm_all_persons"

ATLAS_CLIENT = "atlas"
ATLAS_SCOPES = ("openid", "profile", "groups")
# The relying party's callback (SELF_HOSTING → Bring your own OIDC): Atlas's client has
# this one URL, as its landing URL and as its only redirect URL.
CALLBACK_PATH = "/api/v1/auth/callback"

IMMICH_CLIENT = "immich"
IMMICH_SCOPES = ("openid", "profile", "email")
# SELF_HOSTING → Connecting Immich: exactly these three. The landing URL is the first.
IMMICH_REDIRECT_PATHS = ("/auth/login", "/user-settings")
IMMICH_MOBILE_REDIRECT = "app.immich:///oauth-callback"

# Files under the state directory (secrets/atlas-idp, read by atlas-api) and the
# clients directory (secrets/idp-clients, read by the operator).
IDP_ENV = "idp.env"
GROUPS_FILE = "groups.json"
IMMICH_ENV = "immich.env"
ENV_CLIENT_SECRET = "ATLAS_OIDC_CLIENT_SECRET"
ENV_ADMIN_TOKEN = "ATLAS_IDP_ADMIN_TOKEN"
IMMICH_ENV_CLIENT_SECRET = "OAUTH_CLIENT_SECRET"
# The atlas group (gid 10001) reads these: owner and group read, nothing for others.
SHARED_MODE = 0o640
PRIVATE_MODE = 0o600

# A name Kanidm gives to a group or a service account this module can look up.
_KANIDM_NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# What may be written into an env file unquoted: no space, quote, `#`, `$` or newline.
_ENV_VALUE = re.compile(r"[A-Za-z0-9._~+/=-]+")


@dataclass(frozen=True)
class RolePolicy:
    group: str
    credential_type_minimum: str
    authsession_expiry: int | None = None  # seconds; None leaves Kanidm's default


# Decided 2026-11-16 (PLAN Phase 5): the people who can change things need a second
# factor and a shorter session; children and guests may use a password alone.
ROLE_POLICIES = (
    RolePolicy("atlas_role_owner", "mfa", 28800),
    RolePolicy("atlas_role_admin", "mfa", 28800),
    RolePolicy("atlas_role_adult", "mfa"),
    RolePolicy("atlas_role_child", "any"),
    RolePolicy("atlas_role_guest", "any"),
)


class BootstrapError(RuntimeError):
    """A failed step. The message names the step and holds nothing secret."""

    def __init__(self, step: str, message: str) -> None:
        self.step = step
        self.reason = message
        super().__init__(f"{step}: {message}")


class IdpClient(Protocol):
    """The calls of `KanidmClient` that the bootstrap makes."""

    async def login(self, username: str, password: SecretStr) -> None: ...
    async def get_group(self, name: str) -> Entry | None: ...
    async def create_group(self, name: str) -> None: ...
    async def add_group_attr(self, group: str, attr: str, values: Sequence[str]) -> None: ...
    async def set_group_attr(self, group: str, attr: str, values: Sequence[str]) -> None: ...
    async def add_group_members(self, group: str, members: Sequence[str]) -> None: ...
    async def remove_group_members(self, group: str, members: Sequence[str]) -> None: ...
    async def get_service_account(self, name: str) -> Entry | None: ...
    async def create_service_account(
        self, name: str, *, display_name: str, managed_by: str
    ) -> None: ...
    async def list_api_tokens(self, account: str) -> list[ApiTokenInfo]: ...
    async def issue_api_token(self, account: str, *, label: str, read_write: bool) -> SecretStr: ...
    async def delete_api_token(self, account: str, token_id: str) -> bool: ...
    async def get_oauth2_client(self, name: str) -> Entry | None: ...
    async def create_oauth2_basic_client(
        self, name: str, *, display_name: str, landing_url: str, strict_redirect_uri: bool = True
    ) -> None: ...
    async def update_oauth2_client(
        self,
        name: str,
        *,
        landing_url: str | None = None,
        strict_redirect_uri: bool | None = None,
        legacy_crypto: bool | None = None,
        require_pkce: bool | None = None,
    ) -> None: ...
    async def add_oauth2_redirect_url(self, name: str, url: str) -> None: ...
    async def remove_unwanted_oauth2_redirect_url(self, name: str, url: str) -> None: ...
    async def set_oauth2_scope_map(self, name: str, group: str, scopes: Sequence[str]) -> None: ...
    async def remove_oauth2_scope_map(self, name: str, group: str) -> None: ...
    async def get_oauth2_basic_secret(self, name: str) -> SecretStr | None: ...


class AdminSocketLike(Protocol):
    async def recover_account(self, name: str) -> SecretStr: ...
    async def disable_account(self, name: str) -> None: ...


# Opens a connection to Kanidm, with a bearer token or without one.
ClientFactory = Callable[[SecretStr | None], AbstractAsyncContextManager[IdpClient]]


@dataclass(frozen=True)
class BootstrapConfig:
    idp_public_url: str
    callback_url: str
    immich_url: str | None
    state_dir: Path
    clients_dir: Path
    verify: bool = False
    rotate_token: bool = False

    @property
    def idp_domain(self) -> str:
        """Kanidm's domain, the part of a group's SPN after the `@`."""
        return (urlsplit(self.idp_public_url).hostname or "").lower()

    @property
    def immich_redirects(self) -> tuple[str, ...]:
        assert self.immich_url is not None
        return (
            *(f"{self.immich_url}{path}" for path in IMMICH_REDIRECT_PATHS),
            IMMICH_MOBILE_REDIRECT,
        )

    @property
    def immich_landing(self) -> str:
        assert self.immich_url is not None
        return f"{self.immich_url}{IMMICH_REDIRECT_PATHS[0]}"

    def digest(self) -> str:
        """A fingerprint of every input that decides what the IdP should look like."""
        desired = {
            "atlas_client": ATLAS_CLIENT,
            "callback": self.callback_url,
            "idp": self.idp_public_url,
            "immich": self.immich_url,
            "roles": [
                [p.group, p.credential_type_minimum, p.authsession_expiry] for p in ROLE_POLICIES
            ],
        }
        return hashlib.sha256(json.dumps(desired, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class BootstrapResult:
    skipped: bool  # the fast path: nothing was changed
    token_rotated: bool


def callback_url(public_url: str) -> str:
    return f"{public_url.rstrip('/')}{CALLBACK_PATH}"


@contextlib.contextmanager
def _in_step(step: str) -> Iterator[None]:
    """Turns what a step can raise into a BootstrapError with a message free of secrets."""
    try:
        yield
    except BootstrapError:
        raise
    except KanidmError as exc:
        # Kanidm errors carry the operation and status, and Kanidm's own text only for
        # endpoints that never answer with a credential (see errors.py).
        raise BootstrapError(step, str(exc)) from None
    except OSError as exc:
        where = f" ({exc.filename})" if exc.filename else ""
        raise BootstrapError(
            step, f"{type(exc).__name__}: {exc.strerror or 'I/O error'}{where}"
        ) from None
    except ValueError as exc:
        raise BootstrapError(step, f"invalid value: {exc}") from None


def _env_text(header: str, values: dict[str, str]) -> str:
    for name, value in values.items():
        if not _ENV_VALUE.fullmatch(value):
            raise BootstrapError("files", f"the value of {name} cannot be stored in an env file")
    return header + "".join(f"{name}={value}\n" for name, value in values.items())


IDP_ENV_HEADER = (
    "# Written by `atlas-admin idp-bootstrap`. Secrets: never commit or share this file.\n"
    "# Kanidm's value wins: the bootstrap rewrites this file whenever they differ.\n"
)
IMMICH_ENV_HEADER = (
    "# Written by `atlas-admin idp-bootstrap`. Immich's OAuth2 client secret: paste it into\n"
    "# Immich (Administration > Settings > OAuth). Never commit or share this file.\n"
)


class Bootstrap:
    def __init__(
        self,
        config: BootstrapConfig,
        *,
        socket: AdminSocketLike,
        connect: ClientFactory,
        emit: Callable[[str], None],
    ) -> None:
        self._config = config
        self._socket = socket
        self._connect = connect
        self._emit = emit

    # --- entry point ---------------------------------------------------------------

    async def run(self) -> BootstrapResult:
        config = self._config
        # What the last run left, read before this run touches it.
        previous = read_record(config.state_dir / RECORD_FILE)
        # The first act of every run, the fast path included: from here until its last act
        # the record says "not complete", so that atlas-api cannot start on what an
        # earlier run left behind if this one fails (or is killed) on the way.
        with _in_step("record"):
            self._write_record(complete=False)
        if not (config.verify or config.rotate_token):
            blocker = await self._fast_path_blocker(previous)
            if blocker is None:
                self._emit("up to date: nothing to change")
                await self._disable_admin()
                with _in_step("record"):
                    self._write_record(complete=True)
                return BootstrapResult(skipped=True, token_rotated=False)
            self._emit(f"full run: {blocker}")
        else:
            self._emit("full run: requested")
        rotated = await self._full_run()
        return BootstrapResult(skipped=False, token_rotated=rotated)

    # --- the fast path -------------------------------------------------------------

    async def _fast_path_blocker(self, state: dict[str, object] | None) -> str | None:
        """Why the fast path cannot be taken, or None when it can. `state` is the record
        the previous run left."""
        config = self._config
        if state is None:
            return "no bootstrap record yet"
        if not is_current_version(state.get("version")):
            return "the bootstrap changed since the last run"
        if state.get("complete") is not True:
            return "the last run did not finish"
        if state.get("digest") != config.digest():
            return "the settings changed since the last run"
        stored = self._stored_env()
        secret, token = stored.get(ENV_CLIENT_SECRET), stored.get(ENV_ADMIN_TOKEN)
        if not secret or not token:
            return f"{IDP_ENV} is incomplete"
        groups = self._read_groups()
        if groups is None or set(groups) != set(self._group_names()):
            return f"{GROUPS_FILE} is missing or incomplete"
        if config.immich_url is not None and read_text(config.clients_dir / IMMICH_ENV) is None:
            return f"{IMMICH_ENV} is missing"
        try:
            async with self._connect(SecretStr(token)) as client:
                # Only the groups the console manages: it cannot see `atlas_users`, and
                # that it sees the others at all shows it is still their manager.
                for name, uuid in sorted(groups.items()):
                    if name == USERS_GROUP:
                        continue
                    entry = await client.get_group(name)
                    if entry is None or entry.uuid != uuid:
                        return f"group {name} does not match {GROUPS_FILE}"
        except KanidmAuthError:
            return "the stored console token no longer works"
        except (KanidmError, OSError, ValueError):
            # The full run that follows reports the real problem, if there is one.
            return "Kanidm could not be checked with the stored console token"
        return None

    # --- the full run --------------------------------------------------------------

    async def _full_run(self) -> bool:
        failure: BaseException | None = None
        try:
            with _in_step("recover"):
                password = await self._socket.recover_account(ADMIN_ACCOUNT)
            self._emit(f"account {ADMIN_ACCOUNT} recovered")
            async with self._connect(None) as admin:
                with _in_step("login"):
                    await admin.login(ADMIN_ACCOUNT, password)
                del password
                rotated = await self._steps(admin)
        except BaseException as exc:
            failure = exc
            raise
        finally:
            try:
                await self._disable_admin()
            except BootstrapError as disable_error:
                if failure is None:
                    raise
                # The run already failed; say that the account may still be enabled.
                self._emit(f"WARNING {disable_error.reason}; it stays enabled until the next run")
        with _in_step("record"):
            self._write_record(complete=True)  # the last act: nothing after it can fail the run
        self._emit("done")
        return rotated

    async def _steps(self, admin: IdpClient) -> bool:
        config = self._config
        with _in_step("groups"):
            groups = await self._step_groups(admin)
        with _in_step("all-persons"):
            await self._step_all_persons(admin)
        with _in_step("console"):
            await self._step_console(admin)
        if config.verify:
            with _in_step("strays"):
                await self._step_strays(admin)
        with _in_step("token"):
            token, rotated = await self._step_token(admin)
        with _in_step("client-atlas"):
            secret = await self._step_client(
                admin,
                ATLAS_CLIENT,
                display_name="Atlas",
                landing=config.callback_url,
                redirects=(config.callback_url,),
                group=USERS_GROUP,
                scopes=ATLAS_SCOPES,
            )
        if config.immich_url is not None:
            with _in_step("client-immich"):
                immich_secret = await self._step_client(
                    admin,
                    IMMICH_CLIENT,
                    display_name="Immich",
                    landing=config.immich_landing,
                    redirects=config.immich_redirects,
                    group=IMMICH_GROUP,
                    scopes=IMMICH_SCOPES,
                )
                text = _env_text(IMMICH_ENV_HEADER, {IMMICH_ENV_CLIENT_SECRET: immich_secret})
                state = write_if_different(config.clients_dir / IMMICH_ENV, text, mode=PRIVATE_MODE)
                self._emit(f"file clients/{IMMICH_ENV} {state}")
        with _in_step("files"):
            self._step_files(groups, secret, token)
        return rotated

    # --- step 5: groups ------------------------------------------------------------

    def _group_names(self) -> list[str]:
        names = [USERS_GROUP, *(p.group for p in ROLE_POLICIES)]
        if self._config.immich_url is not None:
            names.append(IMMICH_GROUP)
        return names

    def _managed_group_names(self) -> list[str]:
        """The groups the console changes members of: the role groups and immich_users."""
        return [name for name in self._group_names() if name != USERS_GROUP]

    async def _ensure_group(self, admin: IdpClient, name: str) -> Entry:
        entry = await admin.get_group(name)
        state = "kept"
        if entry is None:
            await admin.create_group(name)
            entry = await admin.get_group(name)
            state = "created"
        if entry is None or entry.uuid is None or not _UUID.fullmatch(entry.uuid):
            raise BootstrapError("groups", f"group {name} has no usable UUID")
        self._emit(f"group {name} {entry.uuid} {state}")
        return entry

    async def _ensure_policy(
        self, admin: IdpClient, entry: Entry, credential_type_minimum: str, expiry: int | None
    ) -> None:
        name = entry.name or ""
        updated = False
        if "account_policy" not in entry.attrs.get("class", ()):
            await admin.add_group_attr(name, "class", ["account_policy"])
            updated = True
        if entry.first("credential_type_minimum") != credential_type_minimum:
            await admin.set_group_attr(name, "credential_type_minimum", [credential_type_minimum])
            updated = True
        if expiry is not None and entry.first("authsession_expiry") != str(expiry):
            await admin.set_group_attr(name, "authsession_expiry", [str(expiry)])
            updated = True
        detail = credential_type_minimum + (f", session {expiry} s" if expiry is not None else "")
        self._emit(f"policy {name} {detail} {'updated' if updated else 'kept'}")

    async def _step_groups(self, admin: IdpClient) -> dict[str, str]:
        domain = self._config.idp_domain
        uuids: dict[str, str] = {}
        users = await self._ensure_group(admin, USERS_GROUP)
        uuids[USERS_GROUP] = users.uuid or ""
        for role in ROLE_POLICIES:
            entry = await self._ensure_group(admin, role.group)
            uuids[role.group] = entry.uuid or ""
            await self._ensure_policy(
                admin, entry, role.credential_type_minimum, role.authsession_expiry
            )
        # The role groups are members of atlas_users: one group lists every person.
        members = set((await admin.get_group(USERS_GROUP) or users).attrs.get("member", ()))
        missing = [p.group for p in ROLE_POLICIES if f"{p.group}@{domain}" not in members]
        if missing:
            await admin.add_group_members(USERS_GROUP, missing)
        self._emit(f"group {USERS_GROUP} members {'updated' if missing else 'kept'}")
        if self._config.immich_url is not None:
            entry = await self._ensure_group(admin, IMMICH_GROUP)
            uuids[IMMICH_GROUP] = entry.uuid or ""
        return uuids

    # --- step 6: idm_all_persons ---------------------------------------------------

    async def _step_all_persons(self, admin: IdpClient) -> None:
        # Kanidm applies the strictest policy of a person's groups. Lowering the default
        # (mfa) before every role group carries its own would let people in without one.
        # Kanidm does not check this order (it answers 200), so this does.
        for role in ROLE_POLICIES:
            entry = await admin.get_group(role.group)
            carries = (
                entry is not None
                and "account_policy" in entry.attrs.get("class", ())
                and entry.first("credential_type_minimum") == role.credential_type_minimum
                and (
                    role.authsession_expiry is None
                    or entry.first("authsession_expiry") == str(role.authsession_expiry)
                )
            )
            if not carries:
                raise BootstrapError(
                    "all-persons",
                    f"group {role.group} does not carry its credential policy, so "
                    f"{ALL_PERSONS_GROUP} is left as it is",
                )
        everyone = await admin.get_group(ALL_PERSONS_GROUP)
        if everyone is None:
            raise BootstrapError("all-persons", f"group {ALL_PERSONS_GROUP} does not exist")
        await self._ensure_policy(admin, everyone, "any", None)

    # --- step 7: the console's service account -------------------------------------

    async def _step_console(self, admin: IdpClient) -> None:
        domain = self._config.idp_domain
        manager = await admin.get_group(CONSOLE_MANAGER_GROUP)
        if manager is None or manager.uuid is None:
            raise BootstrapError("console", f"group {CONSOLE_MANAGER_GROUP} does not exist")
        entry = await admin.get_service_account(CONSOLE_ACCOUNT)
        state = "kept"
        if entry is None:
            # Created managed by idm_admins, before it joins the two admin groups: an
            # account managed by a group it is itself in could change its own entry.
            await admin.create_service_account(
                CONSOLE_ACCOUNT, display_name="Atlas console", managed_by=CONSOLE_MANAGER_GROUP
            )
            entry = await admin.get_service_account(CONSOLE_ACCOUNT)
            state = "created"
        if entry is None or entry.uuid is None:
            raise BootstrapError("console", f"service account {CONSOLE_ACCOUNT} was not created")
        if entry.first("entry_managed_by") != manager.uuid:
            raise BootstrapError(
                "console",
                f"service account {CONSOLE_ACCOUNT} exists but is not managed by "
                f"{CONSOLE_MANAGER_GROUP}; rename or remove it in Kanidm",
            )
        self._emit(f"service account {CONSOLE_ACCOUNT} {entry.uuid} {state}")
        for group in CONSOLE_GROUPS:
            members = await admin.get_group(group)
            if members is None:
                raise BootstrapError("console", f"group {group} does not exist")
            joined = f"{CONSOLE_ACCOUNT}@{domain}" in members.attrs.get("member", ())
            if not joined:
                await admin.add_group_members(group, [CONSOLE_ACCOUNT])
            self._emit(f"group {group} member {CONSOLE_ACCOUNT} {'kept' if joined else 'updated'}")
        await self._step_managers(admin, entry.uuid)
        await self._leave_former_groups(admin)

    async def _step_managers(self, admin: IdpClient, console_uuid: str) -> None:
        """The console manages each of its groups, and every one is read back.

        Writes only on a difference (idempotent, so a run after a failure resumes). The
        read-back is a second pass over all of them, because the console leaves
        idm_group_admins right after: Kanidm answers 200 to a write it ignored.
        """
        names = self._managed_group_names()
        for name in names:
            entry = await admin.get_group(name)
            if entry is None:
                raise BootstrapError("console", f"group {name} does not exist")
            if entry.first("entry_managed_by") != console_uuid:
                await admin.set_group_attr(name, "entry_managed_by", [console_uuid])
        for name in names:
            entry = await admin.get_group(name)
            if entry is None or entry.first("entry_managed_by") != console_uuid:
                raise BootstrapError(
                    "console", f"group {name} is not managed by {CONSOLE_ACCOUNT} after the write"
                )
        self._emit(f"groups managed by {CONSOLE_ACCOUNT}: {', '.join(names)}")

    async def _leave_former_groups(self, admin: IdpClient) -> None:
        domain = self._config.idp_domain
        for group in FORMER_CONSOLE_GROUPS:
            members = await admin.get_group(group)
            if members is None:
                raise BootstrapError("console", f"group {group} does not exist")
            if f"{CONSOLE_ACCOUNT}@{domain}" in members.attrs.get("member", ()):
                await admin.remove_group_members(group, [CONSOLE_ACCOUNT])
                again = await admin.get_group(group)
                if again is None or f"{CONSOLE_ACCOUNT}@{domain}" in again.attrs.get("member", ()):
                    raise BootstrapError(
                        "console", f"{CONSOLE_ACCOUNT} is still a member of {group} after removal"
                    )
                self._emit(f"group {group} member {CONSOLE_ACCOUNT} removed")
            else:
                self._emit(f"group {group} member {CONSOLE_ACCOUNT} absent")

    async def _non_person(self, admin: IdpClient, name: str) -> tuple[str, str] | None:
        """(kind, UUID) of a group or a service account called `name`, else None.

        A member that is neither is taken for a person: the client has no person calls
        (Phase 5b-3b adds them), and a name that Kanidm would not give a group or a
        service account is always a person.
        """
        if not _KANIDM_NAME.fullmatch(name):
            return None
        group = await admin.get_group(name)
        if group is not None and group.uuid:
            return "group", group.uuid
        account = await admin.get_service_account(name)
        if account is not None and account.uuid:
            return "service account", account.uuid
        return None

    async def _step_strays(self, admin: IdpClient) -> None:
        """--verify: remove members of the six groups that are not persons."""
        removed = 0
        for group in self._managed_group_names():
            entry = await admin.get_group(group)
            if entry is None:
                raise BootstrapError("strays", f"group {group} does not exist")
            for spn in sorted(entry.attrs.get("member", ())):
                member = spn.partition("@")[0]
                found = await self._non_person(admin, member)
                if found is None:
                    continue
                await admin.remove_group_members(group, [member])
                self._emit(f"group {group} member {member} {found[1]} removed (not a person)")
                removed += 1
        if not removed:
            self._emit("groups hold persons only")

    # --- step 8: the console's token -----------------------------------------------

    async def _token_works(self, token: SecretStr) -> bool:
        try:
            async with self._connect(token) as client:
                # A group the console manages: it cannot see atlas_users any more.
                return await client.get_group(ROLE_POLICIES[0].group) is not None
        except (KanidmAuthError, KanidmPermissionDenied):
            return False

    async def _ours(self, admin: IdpClient) -> list[ApiTokenInfo]:
        tokens = await admin.list_api_tokens(CONSOLE_ACCOUNT)
        return [t for t in tokens if t.label == CONSOLE_TOKEN_LABEL]

    async def _step_token(self, admin: IdpClient) -> tuple[SecretStr, bool]:
        """Exactly one token with our label, and it is the one in idp.env."""
        config = self._config
        stored_text = self._stored_env().get(ENV_ADMIN_TOKEN)
        stored = SecretStr(stored_text) if stored_text else None
        ours = await self._ours(admin)
        if stored is not None and not config.rotate_token:
            stored_id = api_token_id(stored)
            if stored_id in {t.token_id for t in ours} and await self._token_works(stored):
                for extra in ours:
                    if extra.token_id != stored_id and await admin.delete_api_token(
                        CONSOLE_ACCOUNT, extra.token_id
                    ):
                        self._emit(f"token {extra.token_id} removed (a second one with our label)")
                self._emit(f"token {stored_id} kept")
                return stored, False
        new = await admin.issue_api_token(
            CONSOLE_ACCOUNT, label=CONSOLE_TOKEN_LABEL, read_write=True
        )
        old_ids = {t.token_id for t in ours}
        if not await self._token_works(new):
            for fresh in await self._ours(admin):
                if fresh.token_id not in old_ids:
                    await admin.delete_api_token(CONSOLE_ACCOUNT, fresh.token_id)
            raise BootstrapError("token", "the new console token was refused by Kanidm")
        # Stored before the old ones are deleted: at every moment one stored token works.
        values = {ENV_ADMIN_TOKEN: new.get_secret_value()}
        previous_secret = self._stored_env().get(ENV_CLIENT_SECRET)
        if previous_secret:
            values = {ENV_CLIENT_SECRET: previous_secret, **values}
        write_atomic(
            config.state_dir / IDP_ENV, _env_text(IDP_ENV_HEADER, values), mode=SHARED_MODE
        )
        for old in sorted(old_ids):
            await admin.delete_api_token(CONSOLE_ACCOUNT, old)
        new_id = api_token_id(new) or "(id unknown)"
        self._emit(f"token {new_id} created" + (f", replaced {len(old_ids)}" if old_ids else ""))
        # "Rotated" means an older token was replaced: a process that holds it needs a restart.
        return new, bool(old_ids)

    # --- steps 9 and 10: OAuth2 clients --------------------------------------------

    async def _step_client(
        self,
        admin: IdpClient,
        name: str,
        *,
        display_name: str,
        landing: str,
        redirects: Sequence[str],
        group: str,
        scopes: Sequence[str],
    ) -> str:
        config = self._config
        entry = await admin.get_oauth2_client(name)
        created = entry is None
        if entry is None:
            await admin.create_oauth2_basic_client(
                name, display_name=display_name, landing_url=landing, strict_redirect_uri=True
            )
            entry = await admin.get_oauth2_client(name)
        if entry is None:
            raise BootstrapError(f"client-{name}", f"client {name} was not created")
        changes: list[str] = []

        # Enforced on every full run, also on a client someone created by hand. An
        # absent attribute means the safe default: PKCE required, legacy crypto off.
        new_landing = landing if entry.first("oauth2_rs_origin_landing") != landing else None
        new_strict = True if entry.first("oauth2_strict_redirect_uri") != "true" else None
        legacy = entry.first("oauth2_jwt_legacy_crypto_enable")
        new_legacy = False if legacy not in (None, "false") else None
        no_pkce = entry.first("oauth2_allow_insecure_client_disable_pkce")
        new_pkce = True if no_pkce not in (None, "false") else None
        if any(v is not None for v in (new_landing, new_strict, new_legacy, new_pkce)):
            await admin.update_oauth2_client(
                name,
                landing_url=new_landing,
                strict_redirect_uri=new_strict,
                legacy_crypto=new_legacy,
                require_pkce=new_pkce,
            )
            changes += [
                label
                for label, value in (
                    ("landing URL", new_landing),
                    ("strict redirect", new_strict),
                    ("legacy crypto off", new_legacy),
                    ("PKCE required", new_pkce),
                )
                if value is not None
            ]

        have, want = set(entry.attrs.get("oauth2_rs_origin", ())), set(redirects)
        for url in sorted(want - have):
            await admin.add_oauth2_redirect_url(name, url)
            changes.append(f"redirect added {url}")
        for url in sorted(have - want):
            await admin.remove_unwanted_oauth2_redirect_url(name, url)
            changes.append(f"redirect removed {url}")

        spn = f"{group}@{config.idp_domain}"
        maps = entry.scope_maps
        if maps.get(spn) != frozenset(scopes):
            await admin.set_oauth2_scope_map(name, group, scopes)
            changes.append(f"scope map {group}")
        if config.verify:
            # Only on a verify run, and only on our own clients: a scope map nobody here
            # set would let that group sign in to the client.
            for other in sorted(set(maps) - {spn}):
                await admin.remove_oauth2_scope_map(name, other.partition("@")[0])
                changes.append(f"scope map removed {other}")

        secret = await admin.get_oauth2_basic_secret(name)
        if secret is None:
            raise BootstrapError(
                f"client-{name}", f"Kanidm gave no secret for client {name} (missing or unreadable)"
            )
        state = "created" if created else ("updated" if changes else "kept")
        self._emit(f"client {name} {state}" + (f" ({'; '.join(changes)})" if changes else ""))
        return secret.get_secret_value()

    # --- step 11: files ------------------------------------------------------------

    def _step_files(self, groups: dict[str, str], secret: str, token: SecretStr) -> None:
        config = self._config
        text = json.dumps(groups, indent=2, sort_keys=True) + "\n"
        state = write_if_different(config.state_dir / GROUPS_FILE, text, mode=SHARED_MODE)
        self._emit(f"file {GROUPS_FILE} {state}")
        env = _env_text(
            IDP_ENV_HEADER, {ENV_CLIENT_SECRET: secret, ENV_ADMIN_TOKEN: token.get_secret_value()}
        )
        state = write_if_different(config.state_dir / IDP_ENV, env, mode=SHARED_MODE)
        self._emit(f"file {IDP_ENV} {state}")

    # --- step 12: disable idm_admin ------------------------------------------------

    async def _disable_admin(self) -> None:
        try:
            await self._socket.disable_account(ADMIN_ACCOUNT)
        except KanidmError as exc:
            raise BootstrapError(
                "disable", f"{ADMIN_ACCOUNT} could not be disabled ({exc.operation})"
            ) from None
        self._emit(f"account {ADMIN_ACCOUNT} disabled")

    # --- state and stored values ---------------------------------------------------

    def _write_record(self, *, complete: bool) -> None:
        write_atomic(
            self._config.state_dir / RECORD_FILE,
            record_text(digest=self._config.digest(), complete=complete),
            mode=SHARED_MODE,
        )

    def _read_groups(self) -> dict[str, str] | None:
        text = read_text(self._config.state_dir / GROUPS_FILE)
        if text is None:
            return None
        try:
            data = json.loads(text)
        except ValueError:
            return None
        if not isinstance(data, dict) or not all(
            isinstance(k, str) and isinstance(v, str) and _UUID.fullmatch(v)
            for k, v in data.items()
        ):
            return None
        return {str(k): str(v) for k, v in data.items()}

    def _stored_env(self) -> dict[str, str]:
        path = self._config.state_dir / IDP_ENV
        if read_text(path) is None:
            return {}
        return {k: v for k, v in dotenv_values(path).items() if v}
