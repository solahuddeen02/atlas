"""Kanidm's admin unix socket: RecoverAccount and DisableAccount (PLAN Phase 5b-1).

The wire format, as observed on Kanidm 1.12.0:

- A request is one JSON value with no framing and no terminator, e.g.
  ``{"RecoverAccount":{"name":"idm_admin"}}``.
- The answer is one JSON value, also without a newline:
  ``{"RecoverAccount":{"password":"..."}}``, ``"Success"`` or ``"Error"``.
- Kanidm keeps the connection open after answering, so the end of the answer is the
  end of the JSON value, not EOF.
- ``"Error"`` carries no reason (an unknown account is the usual one).
- Kanidm sends nothing at all for a request it cannot parse. Only a timeout ends that.

RecoverAccount sets a new random password every time, so the previous one stops
working. It also re-enables an account that DisableAccount disabled.

Access is by file permission: in the bundled stack the socket is
``/data/kanidmd.sock``, mode 0750 in a 0700 directory, both owned by the idp user.
Only root or that user can connect.

Both answers are treated as secret-bearing: no error carries any byte of a reply.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import logging
import re
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from atlas.kernel.identity.kanidm.errors import (
    KanidmAdminSocketError,
    KanidmProtocolError,
    KanidmUnavailable,
)

logger = logging.getLogger("atlas.identity.kanidm")

MAX_REPLY_BYTES = 64 * 1024
_NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_decoder = json.JSONDecoder()


def _decode(buffer: bytes) -> tuple[str, Any]:
    """("incomplete" | "complete" | "trailing" | "invalid", value) for the bytes so far.

    Returns a state instead of raising: json's error object holds the whole document.
    """
    try:
        text = buffer.decode()
    except UnicodeDecodeError as exc:
        # A multi-byte character cut by the chunk boundary is still incomplete.
        return ("incomplete" if exc.reason == "unexpected end of data" else "invalid"), None
    stripped = text.strip()
    if not stripped:
        return "incomplete", None
    try:
        value, end = _decoder.raw_decode(stripped)
    except ValueError:
        return "incomplete", None
    return ("complete" if end == len(stripped) else "trailing"), value


class AdminSocket:
    """Client for Kanidm's admin socket at `path`. One connection per request."""

    def __init__(self, path: Path, *, timeout: float = 10.0) -> None:
        self._path = path
        self._timeout = timeout

    def __repr__(self) -> str:
        return f"AdminSocket({str(self._path)!r})"

    async def recover_account(self, name: str) -> SecretStr:
        """Set a new random password on a built-in account and return it."""
        reply = await self._request("RecoverAccount", name)
        if isinstance(reply, dict) and set(reply) == {"RecoverAccount"}:
            inner = reply["RecoverAccount"]
            if isinstance(inner, dict) and set(inner) == {"password"}:
                password = inner["password"]
                if isinstance(password, str) and password:
                    return SecretStr(password)
        raise self._unexpected("RecoverAccount")

    async def disable_account(self, name: str) -> None:
        """Disable an account: its sessions end and it cannot log in."""
        reply = await self._request("DisableAccount", name)
        if reply != "Success":
            raise self._unexpected("DisableAccount")

    @staticmethod
    def _unexpected(request: str) -> KanidmProtocolError:
        return KanidmProtocolError("unexpected reply", operation=f"admin socket {request}")

    async def _request(self, request: str, name: str) -> Any:
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise ValueError("invalid Kanidm account name")
        operation = f"admin socket {request}"
        payload = json.dumps({request: {"name": name}}, separators=(",", ":")).encode()
        # Outcomes are recorded and raised after the try block, so no error has a
        # __context__ that could hold reply bytes.
        unavailable: str | None = None
        state, value = "incomplete", None
        received = 0
        try:
            async with asyncio.timeout(self._timeout):
                reader, writer = await asyncio.open_unix_connection(str(self._path))
                try:
                    writer.write(payload)
                    await writer.drain()
                    buffer = b""
                    while state == "incomplete":
                        chunk = await reader.read(4096)
                        if not chunk:
                            break
                        buffer += chunk
                        received = len(buffer)
                        if received > MAX_REPLY_BYTES:
                            state = "invalid"
                            break
                        state, value = _decode(buffer)
                finally:
                    writer.close()
                    with contextlib.suppress(OSError):
                        await writer.wait_closed()
        except TimeoutError:
            if received == 0:
                unavailable = f"no reply within {self._timeout:g} s"
        except OSError as exc:
            code = errno.errorcode.get(exc.errno or 0, type(exc).__name__)
            unavailable = f"cannot use the socket ({code})"

        if unavailable is not None:
            logger.warning("%s failed: %s", operation, unavailable)
            raise KanidmUnavailable(unavailable, operation=operation)
        if state != "complete":
            logger.warning("%s failed: unexpected reply", operation)
            raise self._unexpected(request)
        if value == "Error":
            logger.warning("%s -> Error", operation)
            raise KanidmAdminSocketError(
                "Kanidm refused the request (the reason is in Kanidm's log)", operation=operation
            )
        logger.info("%s -> ok", operation)
        return value
