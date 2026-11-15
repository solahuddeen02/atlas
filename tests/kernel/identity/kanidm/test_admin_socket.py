"""The admin-socket client against a fake socket that replays Kanidm 1.12.0's bytes."""

import asyncio
import os
import socket
from pathlib import Path

import pytest
from pydantic import SecretStr

from atlas.kernel.identity.kanidm import (
    AdminSocket,
    KanidmAdminSocketError,
    KanidmProtocolError,
    KanidmUnavailable,
)
from tests.kernel.identity.kanidm.fakes import (
    PASSWORD,
    SocketBehaviour,
    admin_socket,
    reply_with,
    run,
)

RECOVERED = b'{"RecoverAccount":{"password":"' + PASSWORD.encode() + b'"}}'


async def recover(path: Path, behaviour: SocketBehaviour, *, timeout: float = 5.0) -> SecretStr:
    async with admin_socket(path, behaviour):
        return await AdminSocket(path, timeout=timeout).recover_account("idm_admin")


def test_recover_account_wire_format(socket_path: Path) -> None:
    async def scenario() -> tuple[SecretStr, list[bytes]]:
        # Kanidm answers without a newline and leaves the connection open.
        async with admin_socket(socket_path, reply_with(RECOVERED)) as fake:
            password = await AdminSocket(socket_path, timeout=5).recover_account("idm_admin")
            return password, fake.requests

    password, requests = run(scenario())
    assert requests == [b'{"RecoverAccount":{"name":"idm_admin"}}']
    assert isinstance(password, SecretStr)
    assert password.get_secret_value() == PASSWORD
    assert PASSWORD not in repr(password)


def test_disable_account_wire_format(socket_path: Path) -> None:
    async def scenario() -> list[bytes]:
        async with admin_socket(socket_path, reply_with(b'"Success"')) as fake:
            await AdminSocket(socket_path, timeout=5).disable_account("idm_admin")
            return fake.requests

    assert run(scenario()) == [b'{"DisableAccount":{"name":"idm_admin"}}']


def test_a_reply_split_across_chunks_is_reassembled(socket_path: Path) -> None:
    chunks = (RECOVERED[:9], RECOVERED[9:40], RECOVERED[40:])
    password = run(recover(socket_path, reply_with(*chunks, pause=0.02)))
    assert password.get_secret_value() == PASSWORD


def test_a_reply_followed_by_eof_is_accepted(socket_path: Path) -> None:
    password = run(recover(socket_path, reply_with(RECOVERED, close=True)))
    assert password.get_secret_value() == PASSWORD


def test_the_error_reply(socket_path: Path) -> None:
    # What Kanidm sends for an unknown account: seven bytes, no reason.
    with pytest.raises(KanidmAdminSocketError) as caught:
        run(recover(socket_path, reply_with(b'"Error"')))
    assert caught.value.operation == "admin socket RecoverAccount"

    async def disable() -> None:
        async with admin_socket(socket_path, reply_with(b'"Error"')):
            await AdminSocket(socket_path, timeout=5).disable_account("nosuch")

    with pytest.raises(KanidmAdminSocketError):
        run(disable())


def test_silence_is_unavailable_after_the_timeout(socket_path: Path) -> None:
    # Kanidm sends nothing for a request it cannot parse and keeps the connection open.
    with pytest.raises(KanidmUnavailable, match=r"no reply within 0\.3 s"):
        run(recover(socket_path, reply_with(), timeout=0.3))


@pytest.mark.parametrize(
    "reply",
    [
        b'"Success"',  # the answer to the other request
        b'{"RecoverAccount":{}}',
        b'{"RecoverAccount":{"password":""}}',
        b'{"RecoverAccount":{"password":["a"]}}',
        b'{"SomethingElse":{"password":"x"}}',
        b"[1, 2, 3]",
        b"null",
    ],
)
def test_an_unexpected_recover_reply_is_a_protocol_error(reply: bytes, socket_path: Path) -> None:
    with pytest.raises(KanidmProtocolError):
        run(recover(socket_path, reply_with(reply)))


@pytest.mark.parametrize("reply", [b'"Succes', b"not json at all", b"\xff\xfe\x00garbage"])
def test_an_unparsable_reply_is_a_protocol_error(reply: bytes, socket_path: Path) -> None:
    with pytest.raises(KanidmProtocolError):
        run(recover(socket_path, reply_with(reply), timeout=0.3))
    with pytest.raises(KanidmProtocolError):
        run(recover(socket_path, reply_with(reply, close=True)))


def test_an_unexpected_disable_reply_is_a_protocol_error(socket_path: Path) -> None:
    async def scenario() -> None:
        async with admin_socket(socket_path, reply_with(RECOVERED)):
            await AdminSocket(socket_path, timeout=5).disable_account("idm_admin")

    with pytest.raises(KanidmProtocolError):
        run(scenario())


def test_an_oversized_reply_is_a_protocol_error(socket_path: Path) -> None:
    huge = b'"' + b"x" * (70 * 1024)
    with pytest.raises(KanidmProtocolError):
        run(recover(socket_path, reply_with(huge)))


def test_a_missing_socket_is_unavailable(socket_path: Path) -> None:
    with pytest.raises(KanidmUnavailable, match="ENOENT"):
        run(AdminSocket(socket_path, timeout=1).recover_account("idm_admin"))


def test_a_socket_nobody_listens_on_is_unavailable(socket_path: Path) -> None:
    # A socket file left behind by a server that is gone.
    stale = socket.socket(socket.AF_UNIX)
    stale.bind(str(socket_path))
    stale.close()
    with pytest.raises(KanidmUnavailable, match="ECONNREFUSED"):
        run(AdminSocket(socket_path, timeout=1).recover_account("idm_admin"))


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_a_socket_this_user_may_not_open_is_unavailable(socket_path: Path) -> None:
    async def scenario() -> None:
        async with admin_socket(socket_path, reply_with(RECOVERED)):
            # Like any uid but root and the idp user in the bundled stack (mode 0750
            # in a 0700 directory): connect fails with EACCES.
            socket_path.chmod(0o000)
            await AdminSocket(socket_path, timeout=1).recover_account("idm_admin")

    with pytest.raises(KanidmUnavailable, match="EACCES"):
        run(scenario())


@pytest.mark.parametrize("name", ["", "Idm_Admin", 'x"}}', "a b", "a" * 65])
def test_account_names_are_validated(name: str, socket_path: Path) -> None:
    async def scenario() -> list[bytes]:
        async with admin_socket(socket_path, reply_with(RECOVERED)) as fake:
            client = AdminSocket(socket_path, timeout=1)
            with pytest.raises(ValueError):
                await client.recover_account(name)
            with pytest.raises(ValueError):
                await client.disable_account(name)
            await asyncio.sleep(0)
            return fake.requests

    assert run(scenario()) == []
