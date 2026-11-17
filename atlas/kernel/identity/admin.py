"""The identity provider's administration, as Atlas needs it (ADR-001 "Provisioning").

`IdentityAdmin` is what the admin console (Phase 8) and the CLI use to manage the people
of a household in whatever provider holds them. Implementations: `kanidm` (the bundled
IdP, `atlas.kernel.identity.kanidm.admin`) and `fake` (`atlas.kernel.identity.fake`, for
tests). `manual` (bring-your-own OIDC) comes in Phase 5d.

The rules every implementation keeps:

- **Every person is in exactly one role group.** A person with none or several is
  refused by every method but `set_role(..., repair=True)` and `delete`, which can put
  such a person right. `get_person` raises `RoleInvariantError` for them.
- **A person is addressed by a `PersonRef`**, the IdP's own id of the person (the OIDC
  `sub`), never by a username. A username names a new person once.
- **A reset link is a bearer secret.** `ResetLink` shows nothing of it in `repr`, `str`,
  a log line or an error; `reveal()` is the one way to the URL. A link cannot be revoked
  and a new one does not cancel an older one: each works until it is used or expires,
  which is why the lifetime is short and bounded.
- **Nothing here lists sessions** (ADR-001 Gap 10).

No error holds a secret, and no error holds anything from an IdP response beyond a
fixed description.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal, Protocol, runtime_checkable

from pydantic import SecretStr

# --- value types --------------------------------------------------------------------


class Role(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    ADULT = "adult"
    CHILD = "child"
    GUEST = "guest"


class App(StrEnum):
    """An application the IdP's groups give access to."""

    IMMICH = "immich"


# The IdP group of each role, and of each app: the names the IdP bootstrap gives them.
# Atlas finds the groups by UUID (secrets/atlas-idp/groups.json); the names only say
# which entry of that file is which.
ROLE_GROUP_NAMES: dict[Role, str] = {role: f"atlas_role_{role.value}" for role in Role}
APP_GROUP_NAMES: dict[App, str] = {App.IMMICH: "immich_users"}

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


@dataclass(frozen=True)
class PersonRef:
    """The IdP's id of a person: a UUID, and the OIDC `sub` of their logins."""

    uuid: str

    def __post_init__(self) -> None:
        if not isinstance(self.uuid, str) or not _UUID.fullmatch(self.uuid):
            raise ValueError("a PersonRef is a lowercase UUID")

    def __str__(self) -> str:
        return self.uuid


@dataclass(frozen=True)
class Person:
    ref: PersonRef
    username: str
    display_name: str
    role: Role
    apps: frozenset[App] = frozenset()
    # False while the IdP's expiry of the account is in the past (`disable`).
    enabled: bool = True
    account_expire: datetime | None = None


class ResetLink:
    """The URL a person opens to set their credentials, and when it stops working.

    The URL holds a bearer token. `repr`, `str` and `format` say only the expiry.
    """

    __slots__ = ("_url", "expires_at")

    def __init__(self, url: str, expires_at: datetime) -> None:
        self._url = SecretStr(url)
        self.expires_at = expires_at

    def reveal(self) -> str:
        """The URL. For handing over (QR code, link) and for nothing else: never log it."""
        return self._url.get_secret_value()

    def __repr__(self) -> str:
        return f"ResetLink(expires_at={self.expires_at.isoformat()}, url=<redacted>)"

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        return format(repr(self), spec)

    def __reduce__(self) -> str | tuple[object, ...]:
        raise TypeError("a ResetLink is not serialised")


@dataclass(frozen=True)
class Readiness:
    """The answer of `check()`: what is wrong, in sentences with no secret in them."""

    problems: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problems


# --- rules ----------------------------------------------------------------------------

RESET_TTL_DEFAULT = timedelta(minutes=15)
RESET_TTL_MIN = timedelta(minutes=5)
RESET_TTL_MAX = timedelta(hours=1)

_USERNAME = re.compile(r"[a-z][a-z0-9_-]{1,31}")
_RESERVED_USERNAMES = frozenset({"admin", "root", "anonymous", "atlas-console", "system"})
DISPLAY_NAME_MAX = 255


def validate_username(username: str) -> str:
    """The username of a new person, or `InvalidUsername`.

    Stricter than Kanidm on purpose: 2 to 32 lowercase letters, digits, `_` and `-`, a
    letter first, no dot (Kanidm also takes dots, up to 64). It is not lowercased for the
    caller: `Somchai` is refused with a message, not silently changed. Reserved names
    (`admin`, `root`, `anonymous`, `atlas-console`, `system`, and everything `idm_…`)
    are refused. Display names carry Thai and any other script; usernames do not.
    """
    if not isinstance(username, str) or not _USERNAME.fullmatch(username):
        raise InvalidUsername(
            "a username is 2 to 32 lowercase letters, digits, '_' or '-', starting with a letter"
        )
    if username in _RESERVED_USERNAMES or username.startswith("idm_") or _UUID.fullmatch(username):
        raise InvalidUsername("that username is reserved")
    return username


def validate_display_name(display_name: str) -> str:
    """The display name as it is stored: NFC, trimmed. `InvalidDisplayName` if unusable."""
    if not isinstance(display_name, str):
        raise InvalidDisplayName("a display name is text")
    text = unicodedata.normalize("NFC", display_name).strip()
    if not text:
        raise InvalidDisplayName("a display name is not empty")
    if len(text) > DISPLAY_NAME_MAX:
        raise InvalidDisplayName(f"a display name is at most {DISPLAY_NAME_MAX} characters")
    for char in text:
        category = unicodedata.category(char)
        if category[0] == "C" and category != "Cf":  # control, surrogate, private use, unassigned
            raise InvalidDisplayName("a display name has no control characters")
    return text


def check_reset_ttl(ttl: timedelta) -> int:
    """The lifetime in whole seconds, or `InvalidResetTtl`. Nothing is clamped."""
    if not isinstance(ttl, timedelta) or not RESET_TTL_MIN <= ttl <= RESET_TTL_MAX:
        raise InvalidResetTtl(
            f"a reset link lives between {int(RESET_TTL_MIN.total_seconds() // 60)} minutes "
            f"and {int(RESET_TTL_MAX.total_seconds() // 3600)} hour"
        )
    return int(ttl.total_seconds())


# --- errors ---------------------------------------------------------------------------


class IdentityError(Exception):
    """Base class. The message names what failed and holds no secret."""


class PersonNotFound(IdentityError):
    def __init__(self, ref: PersonRef) -> None:
        super().__init__(f"no such person: {ref}")
        self.ref = ref


class PersonExists(IdentityError):
    def __init__(self) -> None:
        super().__init__("a person, group or account with that username already exists")


class InvalidUsername(IdentityError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class InvalidDisplayName(IdentityError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class InvalidResetTtl(IdentityError):
    pass


class PersonDisabled(IdentityError):
    def __init__(self, ref: PersonRef) -> None:
        super().__init__(f"the person is disabled: {ref}")
        self.ref = ref


class RoleInvariantError(IdentityError):
    """The person is in no role group, or in several.

    Every method but `set_role(..., repair=True)` and `delete` refuses such a person.
    """

    def __init__(self, ref: PersonRef, roles: Iterable[Role]) -> None:
        self.ref = ref
        self.roles = tuple(sorted(roles))
        found = ", ".join(self.roles) if self.roles else "none"
        super().__init__(f"person {ref} must be in exactly one role group; found: {found}")


class PartialFailure(IdentityError):
    """A change stopped half way and could not be undone.

    `state` is what the person is left in: ``"two_roles"`` (the new role was added, the old
    one is still there), ``"no_role"`` (created, and the role could not be added), or
    ``"unverified"`` (the change was made but could not be read back as one role).
    `set_role(ref, role, repair=True)` or `delete` puts it right. The caller must not
    change its own record of the person's role after this.
    """

    def __init__(
        self, ref: PersonRef, state: Literal["two_roles", "no_role", "unverified"], step: str
    ) -> None:
        super().__init__(f"{step} stopped half way for person {ref}: left in state {state}")
        self.ref = ref
        self.state = state
        self.step = step


class IdentityConfigError(IdentityError):
    """A group in groups.json is missing from the IdP, or a setting is wrong.

    Usually the file is older than the IdP: re-run the IdP bootstrap.
    """


class IdentityPermissionDenied(IdentityError):
    """The IdP refused the console's token for this."""


class IdentityRejected(IdentityError):
    """The IdP refused the request's content (the reason is a fixed sentence)."""


class IdentityUnavailable(IdentityError):
    """The IdP could not be reached, failed, or answered with something unexpected."""


# --- the protocol ---------------------------------------------------------------------


@runtime_checkable
class IdentityAdmin(Protocol):
    async def create_person(
        self,
        username: str,
        display_name: str,
        role: Role,
        *,
        apps: Iterable[App] = (),
    ) -> Person:
        """Create a person with `role` (and `apps`).

        `PersonExists`, `InvalidUsername`, `InvalidDisplayName`. If the role cannot be
        added the person is removed again; if that fails too, `PartialFailure("no_role")`.
        """
        ...

    async def get_person(self, ref: PersonRef) -> Person | None:
        """The person, or None. `RoleInvariantError` if they are in none or several roles."""
        ...

    async def set_role(self, ref: PersonRef, role: Role, *, repair: bool = False) -> Person:
        """Move the person to `role`: add the new group first, then remove the old one.

        The person is never in zero role groups and never in a looser policy during the
        change. A failure is undone once; if that fails, `PartialFailure("two_roles")`.
        Without `repair` a person with none or several roles is `RoleInvariantError`.
        """
        ...

    async def set_app_access(self, ref: PersonRef, app: App, allowed: bool) -> Person:
        """Give or take the person's access to an app. Idempotent."""
        ...

    async def issue_credential_reset(
        self, ref: PersonRef, *, ttl: timedelta = RESET_TTL_DEFAULT
    ) -> ResetLink:
        """A link the person opens to set a password and a second factor, or a passkey.

        `ttl` is 5 minutes to 1 hour (`InvalidResetTtl` outside, no clamping).
        `PersonDisabled` for a disabled person.
        """
        ...

    async def disable(self, ref: PersonRef) -> Person:
        """End the person's logins and refuse their existing IdP sessions. Idempotent.

        Enabling again makes those sessions valid until their own lifetime: a credential
        change ends them for good (issue a reset link when an account may be compromised).
        """
        ...

    async def enable(self, ref: PersonRef) -> Person:
        """Undo `disable`. Idempotent."""
        ...

    async def delete(self, ref: PersonRef) -> bool:
        """Delete the person. False if there was none. Works on a broken person too."""
        ...

    async def check(self) -> Readiness:
        """Whether the console can do its work: never raises, never holds a secret."""
        ...
