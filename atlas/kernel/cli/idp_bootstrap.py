"""`atlas-admin idp-bootstrap`: set up the bundled IdP (ADR-001, PLAN Phase 5b-2).

Run by the one-shot service `atlas-idp-bootstrap` on every start of the stack, as uid
10002 with no capabilities and on the idp-back network only. The engine and its steps
are in `atlas.kernel.identity.bootstrap`.

Exit status: 0 done; 1 a step failed (idm_admin was disabled again, or the output
says it could not be); 2 settings or paths are wrong; 128 + n when signal n stopped it.
Output holds names, UUIDs and created/kept/updated, never a secret.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

from pydantic import SecretStr

from atlas.kernel.config import BootstrapSettings, ConfigError, load_bootstrap_settings
from atlas.kernel.identity.bootstrap import (
    Bootstrap,
    BootstrapConfig,
    BootstrapError,
    BootstrapResult,
    IdpClient,
    callback_url,
)
from atlas.kernel.identity.kanidm import AdminSocket, KanidmClient

DEFAULT_IDP_URL = "https://idp:8443"
DEFAULT_CA_FILE = Path("/run/secrets/idp-trust/ca.pem")
DEFAULT_SOCKET = Path("/run/atlas-idp-admin/kanidmd.sock")
DEFAULT_STATE_DIR = Path("/run/secrets/atlas-idp")
DEFAULT_CLIENTS_DIR = Path("/run/secrets/idp-clients")

PREFIX = "atlas-admin idp-bootstrap:"
ROTATED_HINT = "restart atlas-api to use the new token: docker compose restart atlas-api"


def _emit(line: str) -> None:
    print(f"{PREFIX} {line}", flush=True)


def _fail(message: str) -> None:
    print(f"{PREFIX} {message}", file=sys.stderr, flush=True)


async def _run(bootstrap: Bootstrap) -> int:
    """Run the bootstrap. SIGTERM and SIGINT cancel it once, so that idm_admin is
    disabled on the way out; a second signal during that cleanup is ignored."""
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    assert task is not None
    received: list[int] = []

    def on_signal(number: int) -> None:
        if not received:
            received.append(number)
            task.cancel()

    for number in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(number, on_signal, number)
    try:
        result: BootstrapResult = await bootstrap.run()
    except asyncio.CancelledError:
        if not received:
            raise
        _fail(f"stopped by signal {received[0]}; the lines above say whether idm_admin is disabled")
        return 128 + received[0]
    except BootstrapError as exc:
        _fail(f"failed at step {exc.step}: {exc.reason}")
        return 1
    except Exception as exc:
        # An error this code did not expect: its type only, because its text could
        # hold anything.
        _fail(f"failed with an unexpected {type(exc).__name__}; see docs/SELF_HOSTING.md")
        return 1
    if result.token_rotated:
        _emit(ROTATED_HINT)
    return 0


def _check_paths(args: argparse.Namespace) -> str | None:
    if not args.ca_file.is_file():
        return f"the CA certificate {args.ca_file} is missing (is secrets/idp-trust mounted?)"
    if not args.socket.exists():
        return f"the admin socket {args.socket} is missing (is idp running, with its volume?)"
    for label, path in (("state", args.state_dir), ("clients", args.clients_dir)):
        if not path.is_dir():
            return f"the {label} directory {path} is missing"
    return None


def _cmd(args: argparse.Namespace) -> int:
    try:
        settings: BootstrapSettings = load_bootstrap_settings()
    except ConfigError as exc:
        _fail(str(exc))
        return 2
    problem = _check_paths(args)
    if problem is not None:
        _fail(problem)
        return 2
    config = BootstrapConfig(
        idp_public_url=settings.idp_public_url,
        callback_url=callback_url(settings.public_url),
        immich_url=settings.immich_public_url,
        state_dir=args.state_dir,
        clients_dir=args.clients_dir,
        verify=args.verify,
        rotate_token=args.rotate_console_token,
    )

    def connect(token: SecretStr | None) -> KanidmClient:
        return KanidmClient(base_url=args.idp_url, ca_file=args.ca_file, token=token)

    def factory(token: SecretStr | None) -> _Session:
        return _Session(connect(token))

    bootstrap = Bootstrap(config, socket=AdminSocket(args.socket), connect=factory, emit=_emit)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    return asyncio.run(_run(bootstrap))


class _Session:
    """`async with` a KanidmClient and get it back typed as the bootstrap's `IdpClient`."""

    def __init__(self, client: KanidmClient) -> None:
        self._client = client

    async def __aenter__(self) -> IdpClient:
        return self._client

    async def __aexit__(self, *exc: object) -> None:
        await self._client.aclose()


def add_commands(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = commands.add_parser(
        "idp-bootstrap",
        help="Bundled IdP: create Atlas's OAuth2 client, service account and role groups.",
        description=(
            "Configure the bundled Kanidm for Atlas (and Immich, with ATLAS_IMMICH_PUBLIC_URL). "
            "Idempotent. Runs as the one-shot service atlas-idp-bootstrap."
        ),
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Do the full run even when nothing changed, and remove scope maps on the "
        "atlas and immich clients that the bootstrap did not set.",
    )
    parser.add_argument(
        "--rotate-console-token",
        action="store_true",
        help="Issue a new token for the console's service account and replace the stored one.",
    )
    parser.add_argument("--idp-url", default=DEFAULT_IDP_URL, help="Kanidm's internal URL.")
    parser.add_argument("--ca-file", type=Path, default=DEFAULT_CA_FILE)
    parser.add_argument("--socket", type=Path, default=DEFAULT_SOCKET)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--clients-dir", type=Path, default=DEFAULT_CLIENTS_DIR)
    parser.set_defaults(handler=_cmd)
