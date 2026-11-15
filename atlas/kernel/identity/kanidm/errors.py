"""Errors raised by the Kanidm client.

Every error names the operation (HTTP method and path *template*, or the admin-socket
request) and, for REST, the status. An error never carries a request body, a header or
a credential.

`detail` is Kanidm's own error text, cut to 200 characters. It is set only for
endpoints whose responses hold no secret. For the endpoints that answer with a
credential (login, API tokens, the OAuth2 client and its secret, the admin socket),
errors carry a fixed description and nothing from the response.
"""

from __future__ import annotations

from typing import Literal

AuthFailure = Literal[
    "not_authenticated",  # 401: no bearer, or one Kanidm does not accept
    "session_expired",  # 401: the bearer's session ended (e.g. the account was disabled)
    "incorrect_password",  # login refused
    "account_expired",  # login refused: the account is disabled or expired
    "denied",  # login refused for another reason
]


class KanidmError(Exception):
    """Base class. `operation` is e.g. ``POST /v1/group/{id}/_attr/{attr}``."""

    def __init__(
        self,
        description: str,
        *,
        operation: str,
        status: int | None = None,
        detail: str | None = None,
    ) -> None:
        self.description = description
        self.operation = operation
        self.status = status
        self.detail = detail
        message = f"{operation}: {description}"
        if status is not None:
            message += f" (HTTP {status})"
        if detail:
            message += f": {detail}"
        super().__init__(message)


class KanidmUnavailable(KanidmError):
    """Kanidm could not be reached, or did not answer in time.

    REST: connection refused, timeout, or a TLS failure (`tls` is True when the
    certificate did not verify against the CA file). Admin socket: the socket is
    missing, refuses the connection or this user, or sent nothing before the timeout.
    Kanidm also stays silent when it cannot parse a request.
    """

    def __init__(self, description: str, *, operation: str, tls: bool = False) -> None:
        super().__init__(description, operation=operation)
        self.tls = tls


class KanidmProtocolError(KanidmError):
    """Kanidm answered with something this client does not understand."""


class KanidmAuthError(KanidmError):
    """Not authenticated (401), or a login that Kanidm refused."""

    def __init__(
        self, description: str, *, operation: str, reason: AuthFailure, status: int | None = None
    ) -> None:
        super().__init__(description, operation=operation, status=status)
        self.reason: AuthFailure = reason


class KanidmPermissionDenied(KanidmError):
    """403: the caller may not do this."""


class KanidmNotFound(KanidmError):
    """404: the object a write refers to does not exist.

    Reading a missing object is not an error: the `get_*` methods return None.
    """


class KanidmConflict(KanidmError):
    """409: an object with this name already exists."""

    def __init__(
        self,
        description: str,
        *,
        operation: str,
        status: int | None = None,
        detail: str | None = None,
        conflicting_attributes: tuple[str, ...] = (),
    ) -> None:
        super().__init__(description, operation=operation, status=status, detail=detail)
        self.conflicting_attributes = conflicting_attributes


class KanidmInvalidRequest(KanidmError):
    """400 or 422: Kanidm rejected the request's content (`detail` says why)."""


class KanidmServerError(KanidmError):
    """5xx. Not always a server fault: Kanidm 1.12.0 answers 500 when a group member
    that does not exist is added (`detail` names the reason)."""


class KanidmAdminSocketError(KanidmError):
    """The admin socket answered ``"Error"``.

    Kanidm gives no reason on the socket (an unknown account is the usual one); the
    reason is in Kanidm's own log.
    """
