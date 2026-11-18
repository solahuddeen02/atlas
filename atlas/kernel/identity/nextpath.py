"""The `next` of a login: where to send the person afterwards (ADR-003 §4).

It must be a path inside Atlas. A login that accepted any address would be an open
redirect: a link on any site could send someone through Atlas's real sign-in page and on
to an attacker's page. So `next` must start with a single `/` (not `//` or `/\\`), have no
scheme and no host, and contain no backslash and no control character. It is decoded
once and checked again, which catches `/%2F/host` and `/%5Chost`. Anything else becomes `/`.
"""

from __future__ import annotations

from urllib.parse import unquote, urlsplit

DEFAULT_NEXT = "/"
MAX_NEXT_LENGTH = 512


def _is_control(char: str) -> bool:
    code = ord(char)
    return code < 0x20 or code == 0x7F or 0x80 <= code < 0xA0


def _acceptable(path: str) -> bool:
    if not path.startswith("/") or path.startswith("//") or path.startswith("/\\"):
        return False
    if "\\" in path or any(_is_control(c) for c in path):
        return False
    parts = urlsplit(path)
    return not parts.scheme and not parts.netloc


def safe_next_path(value: object, default: str = DEFAULT_NEXT) -> str:
    """`value` if it is a path inside Atlas, else `default`."""
    if not isinstance(value, str) or not value or len(value) > MAX_NEXT_LENGTH:
        return default
    if not _acceptable(value):
        return default
    if not _acceptable(unquote(value)):  # decoded once, then judged again
        return default
    return value
