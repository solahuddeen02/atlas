"""Test doubles for the Kanidm client: a scripted REST handler and a fake admin socket.

The byte strings and bodies are the ones a real Kanidm 1.12.0 produced (PLAN Phase
5b-1 probe), with the credentials replaced by test values.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

import httpx
import pytest
from pydantic import SecretStr

from atlas.kernel.identity.kanidm import KanidmClient

T = TypeVar("T")
Handler = Callable[[httpx.Request], httpx.Response]

BASE_URL = "https://idp:8443"


@dataclass(frozen=True)
class TlsMaterial:
    ca_file: Path  # signed the server certificate
    other_ca_file: Path  # a different CA: verification against it must fail
    cert_file: Path  # server certificate for `idp` and `localhost`
    key_file: Path


def fake_secret(tag: str, length: int = 48) -> str:
    """A made-up credential, unique per tag, that no log line contains by chance."""
    return (hashlib.sha256(tag.encode()).hexdigest() * 4)[:length]


def fake_jws(tag: str) -> str:
    return ".".join(fake_secret(f"{tag}-{part}", 24) for part in ("header", "payload", "sig"))


SESSION = fake_jws("session")
BEARER = fake_jws("bearer")
PASSWORD = fake_secret("password")
COOKIE_VALUE = fake_secret("cookie", 32)
COOKIE = f"auth-session-id={COOKIE_VALUE}; HttpOnly; Secure; SameSite=Strict"


def run(coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


def make_client(handler: Handler, ca_file: Path, *, token: str | None = None) -> KanidmClient:
    return KanidmClient(
        base_url=BASE_URL,
        ca_file=ca_file,
        token=SecretStr(token) if token is not None else None,
        transport=httpx.MockTransport(handler),
    )


def json_response(
    status: int, body: object = None, headers: dict[str, str] | None = None
) -> httpx.Response:
    """Always a JSON body, also for null (httpx sends no body for `json=None`)."""
    return httpx.Response(
        status,
        content=json.dumps(body).encode(),
        headers={"content-type": "application/json", **(headers or {})},
    )


def body_of(request: httpx.Request) -> Any:
    return json.loads(request.content) if request.content else None


class Recorder:
    """A handler that records every request and answers from a fixed function."""

    def __init__(self, respond: Handler | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self._respond = respond or (lambda request: json_response(200))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._respond(request)

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]


class LoginScript:
    """Kanidm's three login steps. `final` replaces the answer to the cred step."""

    def __init__(
        self,
        *,
        password: str = PASSWORD,
        bearer: str = BEARER,
        final: httpx.Response | None = None,
        then: Handler | None = None,
    ) -> None:
        self.password = password
        self.bearer = bearer
        self.final = final
        self.then = then
        self.steps: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path != "/v1/auth":
            assert self.then is not None, f"unexpected request {request.method} {request.url.path}"
            return self.then(request)
        self.steps.append(request)
        step = body_of(request)["step"]
        headers = {"x-kanidm-auth-session-id": SESSION, "set-cookie": COOKIE}
        if "init2" in step:
            return httpx.Response(
                200, headers=headers, json={"sessionid": "s-1", "state": {"choose": ["password"]}}
            )
        if request.headers.get("x-kanidm-auth-session-id") != SESSION:
            return httpx.Response(
                500, json={"invalidauthstate": "session id not present in cred presented"}
            )
        if step.get("begin") == "password":
            return httpx.Response(
                200, headers=headers, json={"sessionid": "s-1", "state": {"continue": ["password"]}}
            )
        if self.final is not None:
            return self.final
        if step["cred"]["password"] != self.password:
            return httpx.Response(
                200, json={"sessionid": "s-1", "state": {"denied": "incorrect password"}}
            )
        return httpx.Response(200, json={"sessionid": "s-1", "state": {"success": self.bearer}})


# --- admin socket ---------------------------------------------------------------

SocketBehaviour = Callable[[bytes, asyncio.StreamWriter], Awaitable[None]]


def reply_with(*chunks: bytes, close: bool = False, pause: float = 0.0) -> SocketBehaviour:
    """Answer any request with these chunks. Like Kanidm, keep the connection open."""

    async def behave(request: bytes, writer: asyncio.StreamWriter) -> None:
        for chunk in chunks:
            writer.write(chunk)
            await writer.drain()
            if pause:
                await asyncio.sleep(pause)
        if close:
            writer.close()

    return behave


class FakeAdminSocket:
    def __init__(self) -> None:
        self.requests: list[bytes] = []


@contextlib.asynccontextmanager
async def admin_socket(path: Path, behaviour: SocketBehaviour) -> AsyncIterator[FakeAdminSocket]:
    """A unix socket server that reads one JSON request per connection."""
    fake = FakeAdminSocket()
    writers: list[asyncio.StreamWriter] = []

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writers.append(writer)
        request = b""
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                return
            request += chunk
            try:
                json.loads(request)
            except ValueError:
                continue
            break
        fake.requests.append(request)
        await behaviour(request, writer)

    server = await asyncio.start_unix_server(serve, path=str(path))
    try:
        yield fake
    finally:
        for writer in writers:
            writer.close()
        server.close()
        await server.wait_closed()


# --- secret checks --------------------------------------------------------------


def exception_chain(exc: BaseException) -> Iterator[BaseException]:
    seen: set[int] = set()
    todo: list[BaseException | None] = [exc]
    while todo:
        current = todo.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        todo += [current.__cause__, current.__context__]


def assert_no_secret(
    secrets: list[str], caplog: pytest.LogCaptureFixture, *objects: object
) -> None:
    """None of `secrets` is in any log record or in any object's str/repr.

    For an exception: also its attributes and every exception reachable through
    __cause__ and __context__.
    """
    texts: list[tuple[str, str]] = []
    for record in caplog.records:
        texts.append((f"log record {record.name}", record.getMessage()))
        texts.append((f"log args {record.name}", repr(record.args)))
        if record.exc_info:
            texts.append((f"log exc_info {record.name}", repr(record.exc_info)))
    texts.append(("caplog.text", caplog.text))
    for obj in objects:
        if isinstance(obj, BaseException):
            for link in exception_chain(obj):
                label = type(link).__name__
                texts.append((f"str({label})", str(link)))
                texts.append((f"repr({label})", repr(link)))
                texts.append((f"vars({label})", repr(vars(link))))
                texts.append((f"args({label})", repr(link.args)))
        else:
            texts.append((f"str({type(obj).__name__})", str(obj)))
            texts.append((f"repr({type(obj).__name__})", repr(obj)))
    for secret in secrets:
        for where, text in texts:
            assert secret not in text, f"a secret appears in {where}"


def debug_logging(caplog: pytest.LogCaptureFixture) -> None:
    """Capture everything, including httpx and httpcore, at DEBUG."""
    caplog.set_level(logging.DEBUG)
    for name in ("httpx", "httpcore", "httpcore.http11", "httpcore.connection", "asyncio"):
        caplog.set_level(logging.DEBUG, logger=name)
