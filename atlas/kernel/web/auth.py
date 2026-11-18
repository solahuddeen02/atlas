"""What every authenticated request goes through (ADR-003 §4).

`current_session` is the one dependency that turns a cookie into a person. Routes depend
on it (or on `require_step_up`), never on the cookie. Phase 6 wraps the `Session` it returns
into a `Principal`.

For an unsafe method (anything but GET, HEAD and OPTIONS) it also enforces, before the
handler runs:

1. **Origin.** A request with an `Origin` header that is not the public origin is 403, even
   with a valid CSRF token. A request without an `Origin` header is judged by the token.
2. **CSRF.** `X-CSRF-Token` must equal the session's token (constant-time comparison).

The order is Origin, then the session, then the token, so a cross-site request is refused
without touching the database, and a request with no session is a 401, not a 403.

**The client address is for rate limits and the audit log only.** `client_address` below is
never an input to an authorization decision: no role, permission, session or step-up check
reads it. Do not add one.

Failures use the API's error body, `{"error": {"code", "message"}}`, and say nothing about
why a session was refused: not found, revoked, expired and a disabled person all look the same.
"""

from __future__ import annotations

import hmac
import ipaddress
import logging
from collections.abc import Callable
from datetime import timedelta
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from atlas.kernel.config import Settings
from atlas.kernel.identity.cookies import session_cookie_name
from atlas.kernel.identity.ratelimit import RateLimiter
from atlas.kernel.identity.sessions import Session, SessionStore
from atlas.kernel.identity.tokenbox import TokenBox

logger = logging.getLogger("atlas.auth")

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
CSRF_HEADER = "X-CSRF-Token"
_DEFAULT_PORTS = {"http": 80, "https": 443}


class ApiError(Exception):
    """An error with the API's body. `message` is shown to the caller: keep it generic."""

    def __init__(
        self, status_code: int, code: str, message: str, headers: dict[str, str] | None = None
    ) -> None:
        super().__init__(f"{status_code} {code}")
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = headers or {}


async def api_error_handler(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ApiError)
    return JSONResponse(
        {"error": {"code": exc.code, "message": exc.message}},
        status_code=exc.status_code,
        headers=exc.headers,
    )


NOT_AUTHENTICATED = ApiError(401, "not_authenticated", "Sign in to continue.")


def _origin(url: str) -> str | None:
    """`scheme://host[:port]`, lower-case, default port dropped. None if it is not an origin."""
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in _DEFAULT_PORTS or not parts.hostname:
        return None
    if parts.path not in ("", "/") or parts.query or parts.fragment or parts.username:
        return None
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    suffix = "" if port in (None, _DEFAULT_PORTS[parts.scheme]) else f":{port}"
    return f"{parts.scheme}://{host}{suffix}"


def check_origin(request: Request, settings: Settings) -> None:
    """403 for a present `Origin` that is not the public origin. Absent is judged elsewhere."""
    values = request.headers.getlist("origin")
    if not values:
        return
    public = _origin(settings.public_url)
    if len(values) != 1 or public is None or _origin(values[0]) != public:
        raise ApiError(403, "origin_mismatch", "This request did not come from Atlas.")


def client_address(request: Request, settings: Settings) -> str | None:
    """The client's address, for rate-limit keys and the audit log. **Never for authorization.**

    `X-Real-IP` is believed only when the TCP peer is one of `ATLAS_API_TRUSTED_PEERS`
    (loopback and private ranges by default: the bundled proxy overwrites that header, and
    nothing else can reach the API). From any other peer the header is ignored and the peer's
    own address is returned.
    """
    peer = request.client.host if request.client else None
    try:
        peer_ip = ipaddress.ip_address(peer) if peer else None
    except ValueError:
        peer_ip = None
    if peer_ip is None:
        return None
    if any(
        peer_ip in network
        for network in settings.api_trusted_peers
        if peer_ip.version == network.version
    ):
        forwarded = request.headers.get("x-real-ip", "").strip()
        try:
            return str(ipaddress.ip_address(forwarded))
        except ValueError:
            return str(peer_ip)
    return str(peer_ip)


def get_store(request: Request) -> SessionStore:
    """The session store of this app, built on first use (the engine is lazy too)."""
    state = request.app.state
    if getattr(state, "session_store", None) is None:
        settings: Settings = state.settings
        engine: Engine = state.get_engine()
        state.session_store = SessionStore(
            engine,
            settings.session_lifetimes,
            TokenBox(settings.secret_key, settings.secret_key_previous),
            clock=state.clock,
        )
    store: SessionStore = state.session_store
    return store


def get_rate_limiter(request: Request) -> RateLimiter:
    state = request.app.state
    if getattr(state, "rate_limiter", None) is None:
        state.rate_limiter = RateLimiter(state.get_engine(), clock=state.clock)
    limiter: RateLimiter = state.rate_limiter
    return limiter


def current_session(request: Request) -> Session:
    """The live session of this request, or a 401. Unsafe methods also need Origin and CSRF."""
    settings: Settings = request.app.state.settings
    unsafe = request.method not in SAFE_METHODS
    if unsafe:
        check_origin(request, settings)
    token = request.cookies.get(session_cookie_name(secure=settings.cookie_secure))
    if not token:
        raise NOT_AUTHENTICATED
    try:
        session = get_store(request).lookup(token)
    except SQLAlchemyError:
        # Fail closed. Nothing about the failure (it can name a host or a user) is logged.
        logger.warning("session lookup failed: the database is unavailable")
        raise ApiError(503, "unavailable", "Try again in a moment.") from None
    if session is None:
        raise NOT_AUTHENTICATED
    if unsafe:
        sent = request.headers.get(CSRF_HEADER, "")
        stored = session.csrf_token or ""
        if not stored or not hmac.compare_digest(sent.encode("utf-8"), stored.encode("utf-8")):
            raise ApiError(403, "csrf_failed", "The request was refused.")
    return session


def require_step_up(max_age: int | None = None) -> Callable[..., Session]:
    """A dependency: the session, if its `auth_time` is newer than `max_age` seconds.

    `max_age` defaults to `ATLAS_STEP_UP_MAX_AGE`. Otherwise 403 `step_up_required`: the
    client starts a step-up (5c-3) and tries again. Wired for Phase 6's `step_up` permissions;
    nothing calls it yet.
    """

    def dependency(
        request: Request, session: Annotated[Session, Depends(current_session)]
    ) -> Session:
        settings: Settings = request.app.state.settings
        limit = timedelta(seconds=max_age if max_age is not None else settings.step_up_max_age)
        if request.app.state.clock() - session.auth_time > limit:
            raise ApiError(403, "step_up_required", "Confirm it is you to continue.")
        return session

    return dependency
