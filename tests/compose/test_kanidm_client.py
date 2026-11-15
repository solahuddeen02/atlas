"""Compose acceptance test for Phase 5b-1: the Kanidm client against the real bundled IdP.

Run by the `compose` CI job (RUN_COMPOSE_TESTS=1). One throwaway stack with only the
idp service and its two init services; see tests/compose/stack.py.
"""

import json
import time
from collections.abc import Callable

from tests.compose.stack import HEALTHY_TIMEOUT, IMAGE, UP_TIMEOUT, Stack, run

MakeStack = Callable[..., Stack]

IDP_UID = "10002:10002"

# Runs in a one-off Atlas container as the idp user, on idp-back, with the idp volume
# (for the admin socket) and the public CA certificate. It prints outcomes only: the
# recovered password and the bearer never leave the container.
CLIENT_SCRIPT = """
import asyncio, json
from pathlib import Path
from atlas.kernel.identity.kanidm import AdminSocket, KanidmClient, KanidmConflict

GROUP = "atlas_client_test"

async def main():
    out = {}
    # The first credential comes from the admin socket: no bootstrap has run yet.
    password = await AdminSocket(Path("/data/kanidmd.sock")).recover_account("idm_admin")
    async with KanidmClient(base_url="https://idp:8443", ca_file=Path("/ca/ca.pem")) as client:
        await client.login("idm_admin", password)
        out["missing"] = await client.get_group(GROUP)
        await client.create_group(GROUP)
        created = await client.get_group(GROUP)
        out["created"] = [created.kind, created.name, sorted(created.attrs)]
        try:
            await client.create_group(GROUP)
            out["duplicate"] = "no error"
        except KanidmConflict as exc:
            out["duplicate"] = [type(exc).__name__, exc.status, list(exc.conflicting_attributes)]
    print(json.dumps(out))

asyncio.run(main())
"""


def start_idp_only(stack: Stack) -> None:
    """Start idp (and atlas-init, atlas-idp-init, which it depends on) and nothing else."""
    result = stack.compose("up", "-d", "--no-build", "idp", check=False, timeout=UP_TIMEOUT)
    if result.returncode != 0:
        stack.fail(f"`docker compose up idp` exited {result.returncode}\n{result.stderr[-4000:]}")
    deadline = time.monotonic() + HEALTHY_TIMEOUT
    while time.monotonic() < deadline:
        idp = [s for s in stack.states() if s.get("Service") == "idp"]
        if idp and idp[0].get("Health") == "healthy":
            return
        if idp and idp[0].get("State") in ("exited", "restarting"):
            stack.fail(f"idp is {idp[0].get('State')}")
        time.sleep(2)
    stack.fail(f"idp not healthy within {HEALTHY_TIMEOUT}s")


def test_kanidm_client_against_the_real_idp(make_stack: MakeStack) -> None:
    stack = make_stack(idp=True)
    start_idp_only(stack)
    running = {s.get("Service") for s in stack.states() if s.get("State") == "running"}
    assert running == {"idp"}

    result = run(
        [
            "docker",
            "run",
            "--rm",
            "--user",
            IDP_UID,
            "--network",
            f"{stack.project}_idp-back",
            "-v",
            f"{stack.idp_volume}:/data",
            "-v",
            f"{stack.secrets_dir}/idp-trust:/ca:ro",
            "--entrypoint",
            "python",
            IMAGE,
            "-c",
            CLIENT_SCRIPT,
        ],
        check=False,
    )
    if result.returncode != 0:
        stack.fail(f"the client script exited {result.returncode}\n{result.stderr[-4000:]}")
    lines = result.stdout.strip().splitlines()
    assert len(lines) == 1, lines
    assert json.loads(lines[0]) == {
        # GET on a missing object: Kanidm's `200 null`.
        "missing": None,
        "created": ["group", "atlas_client_test", ["class", "name", "spn", "uuid"]],
        # A duplicate create: 409, naming the unique attributes.
        "duplicate": ["KanidmConflict", 409, ["name", "spn"]],
    }
