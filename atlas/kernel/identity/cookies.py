"""The names and attributes of Atlas's cookies (ADR-003 §4).

In production (`Secure`) the cookies are `__Host-` cookies: the browser then accepts them
only with `Secure`, `Path=/` and no `Domain`, so a sibling host cannot set or shadow them.
Browsers refuse a `__Host-` cookie that lacks `Secure`, so outside production (plain
HTTP) the same cookies have no prefix and are not `Secure`. The two always go together:
`secure` decides both the name and the attribute.
"""

from __future__ import annotations

from fastapi import Response

HOST_PREFIX = "__Host-"
SESSION_COOKIE_BASE = "atlas_session"
LOGIN_COOKIE_BASE = "atlas_login"


def cookie_name(base: str, *, secure: bool) -> str:
    return HOST_PREFIX + base if secure else base


def session_cookie_name(*, secure: bool) -> str:
    return cookie_name(SESSION_COOKIE_BASE, secure=secure)


def login_cookie_name(*, secure: bool) -> str:
    return cookie_name(LOGIN_COOKIE_BASE, secure=secure)


def set_cookie(response: Response, name: str, value: str, *, secure: bool, max_age: int) -> None:
    """`HttpOnly; SameSite=Lax; Path=/`, `Secure` when `secure`, and never a `Domain`.

    `Lax` (not `Strict`) because the IdP sends the browser back to Atlas with a top-level
    GET, and the login cookie has to come along with it.
    """
    if secure and not name.startswith(HOST_PREFIX):
        raise ValueError("a Secure cookie of Atlas is a __Host- cookie")
    if not secure and name.startswith(HOST_PREFIX):
        raise ValueError("a __Host- cookie must be Secure")
    response.set_cookie(
        key=name,
        value=value,
        max_age=max_age,
        path="/",
        secure=secure,
        httponly=True,
        samesite="lax",
    )


def clear_cookie(response: Response, name: str, *, secure: bool) -> None:
    response.delete_cookie(key=name, path="/", secure=secure, httponly=True, samesite="lax")
