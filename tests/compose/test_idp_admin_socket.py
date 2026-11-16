"""Compose acceptance test for Phase 5b-2a: Kanidm's admin socket on its own volume, and
the client calls the IdP bootstrap needs, against the real bundled IdP.

Run by the `compose` CI job (RUN_COMPOSE_TESTS=1). One throwaway stack with only the
idp service and its init services; see tests/compose/stack.py. Nothing here prints a
password, token or client secret: the scripts print outcomes only.
"""

import json
import time
from collections.abc import Callable

from tests.compose.stack import HEALTHY_TIMEOUT, IMAGE, UP_TIMEOUT, Stack, run

MakeStack = Callable[..., Stack]

SOCKET_DIR = "/run/atlas-idp-admin"
SOCKET = f"{SOCKET_DIR}/kanidmd.sock"

# Connects to the socket as whoever the container runs as, and prints the outcome.
# DisableAccount on idm_admin is idempotent and harmless here.
SOCKET_PROBE = f"""
import asyncio
from pathlib import Path
from atlas.kernel.identity.kanidm import AdminSocket, KanidmUnavailable

async def main():
    try:
        await AdminSocket(Path("{SOCKET}")).disable_account("idm_admin")
        print("reached")
    except KanidmUnavailable as exc:
        print("refused:", exc.description)

asyncio.run(main())
"""

# G1-G4 against the real server, as the idp user, on idp-back.
ROUND_TRIP = f"""
import asyncio, json
from pathlib import Path
from pydantic import SecretStr
from atlas.kernel.identity.kanidm import AdminSocket, KanidmClient, api_token_id

APP = "app.immich:///oauth-callback"
HTTPS = "https://immich.test.invalid/auth/login"

async def main():
    out = {{}}
    password = await AdminSocket(Path("{SOCKET}")).recover_account("idm_admin")
    try:
        async with KanidmClient(base_url="https://idp:8443", ca_file=Path("/ca/ca.pem")) as c:
            await c.login("idm_admin", password)
            admins = await c.get_group("idm_admins")
            await c.create_group("probe_users")
            group = await c.get_group("probe_users")

            # G4: UUIDs as ids, for reads and for the writes that take a group.
            by_uuid = await c.get_group(group.uuid)
            out["group_by_uuid"] = by_uuid.name
            out["unknown_uuid"] = await c.get_group("0a000000-0000-4000-8000-000000000000")

            await c.create_oauth2_basic_client(
                "probe-client", display_name="Probe", landing_url="https://probe.test.invalid/cb"
            )
            client = await c.get_oauth2_client("probe-client")
            # An OAuth2 client is addressed by name only: Kanidm answers a UUID with
            # `200 null` (a write with 404, or a silent 200), so the client refuses it.
            try:
                await c.get_oauth2_client(client.uuid)
                out["client_uuid_refused"] = False
            except ValueError:
                out["client_uuid_refused"] = True
            out["default_pkce_attr"] = client.first("oauth2_allow_insecure_client_disable_pkce")
            out["default_legacy_attr"] = client.first("oauth2_jwt_legacy_crypto_enable")

            # G2: settings by PATCH, twice (idempotent), and read back.
            for _ in range(2):
                await c.update_oauth2_client(
                    "probe-client",
                    landing_url="https://probe.test.invalid/callback",
                    strict_redirect_uri=True, legacy_crypto=False, require_pkce=True,
                )
            client = await c.get_oauth2_client("probe-client")
            out["settings"] = [
                client.first("oauth2_rs_origin_landing"),
                client.first("oauth2_strict_redirect_uri"),
                client.first("oauth2_jwt_legacy_crypto_enable"),
                client.first("oauth2_allow_insecure_client_disable_pkce"),
            ]
            await c.update_oauth2_client("probe-client", require_pkce=False)
            client = await c.get_oauth2_client("probe-client")
            out["pkce_disabled"] = client.first("oauth2_allow_insecure_client_disable_pkce")
            await c.update_oauth2_client("probe-client", require_pkce=True)

            # G1: an app's custom scheme next to an https URL; http:// never leaves.
            await c.add_oauth2_redirect_url("probe-client", APP)
            await c.add_oauth2_redirect_url("probe-client", HTTPS)
            try:
                await c.add_oauth2_redirect_url("probe-client", "http://immich.test.invalid/x")
                out["http_refused"] = False
            except ValueError:
                out["http_refused"] = True
            client = await c.get_oauth2_client("probe-client")
            out["both_added"] = {{APP, HTTPS}} <= set(client.attrs["oauth2_rs_origin"])
            await c.remove_oauth2_redirect_url("probe-client", HTTPS)
            await c.remove_oauth2_redirect_url("probe-client", HTTPS)
            client = await c.get_oauth2_client("probe-client")
            origins = set(client.attrs["oauth2_rs_origin"])
            out["https_gone_app_kept"] = HTTPS not in origins and APP in origins
            out["strict_after_remove"] = client.first("oauth2_strict_redirect_uri")

            # Scope map set by the group's UUID, read back by SPN.
            await c.set_oauth2_scope_map("probe-client", group.uuid, ["openid", "profile"])
            maps = (await c.get_oauth2_client("probe-client")).scope_maps
            out["scope_maps"] = {{k: sorted(v) for k, v in maps.items()}}
            out["group_spn"] = group.first("spn")

            # G3 and G4: a service account managed by a group's UUID; token delete.
            await c.create_service_account(
                "probe-console", display_name="Probe", managed_by=admins.uuid
            )
            account = await c.get_service_account("probe-console")
            out["managed_by_is_uuid"] = account.attrs["entry_managed_by"] == (admins.uuid,)
            token = await c.issue_api_token(account.uuid, label="probe", read_write=True)
            listed = await c.list_api_tokens(account.uuid)
            out["token_id_matches_list"] = [t.token_id for t in listed] == [api_token_id(token)]
            out["delete_1"] = await c.delete_api_token("probe-console", listed[0].token_id)
            out["delete_2"] = await c.delete_api_token(account.uuid, listed[0].token_id)
            out["tokens_left"] = len(await c.list_api_tokens("probe-console"))
    finally:
        await AdminSocket(Path("{SOCKET}")).disable_account("idm_admin")
    print(json.dumps(out))

asyncio.run(main())
"""


def start_idp_only(stack: Stack) -> None:
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


def in_container(stack: Stack, args: list[str], *, user: str, mount: str, script: str) -> str:
    """Run `python -c script` from the Atlas image as `user` with one volume mount."""
    result = run(
        [
            "docker", "run", "--rm", "--user", user, "--cap-drop", "ALL",
            "--network", f"{stack.project}_idp-back",
            "-v", mount,
            "-v", f"{stack.secrets_dir}/idp-trust:/ca:ro",
            "--entrypoint", "python", IMAGE, "-c", script,
            *args,
        ],
        check=False,
    )  # fmt: skip
    if result.returncode != 0:
        stack.fail(f"the script exited {result.returncode}\n{result.stderr[-4000:]}")
    return result.stdout.strip().splitlines()[-1]


def test_admin_socket_volume(make_stack: MakeStack) -> None:
    stack = make_stack(idp=True)
    start_idp_only(stack)
    admin = stack.idp_admin_volume

    # server.toml names the socket, and the compose file mounts the volume there.
    toml = run(
        [
            "docker", "run", "--rm", "--user", "0:0", "--network", "none", "--entrypoint", "grep",
            "-v", f"{stack.secrets_dir}/idp:/s:ro", IMAGE, "adminbindpath", "/s/server.toml",
        ]
    ).stdout  # fmt: skip
    assert toml.strip() == f'adminbindpath = "{SOCKET}"'
    config = json.loads(stack.compose("config", "--format", "json").stdout)
    mounts = {
        (v["source"], v["target"]): v.get("read_only", False)
        for v in config["services"]["idp"]["volumes"]
    }
    assert mounts[("idp-admin", SOCKET_DIR)] is False
    assert config["volumes"]["idp-admin"].get("external") is None  # an ordinary project volume
    assert config["services"]["atlas-idp-init"]["volumes"]  # prepares it (checked below)

    # atlas-idp-init gave the volume's top directory to the idp user, 0700; the socket
    # is Kanidm's, in it, and the data volume has none.
    stat = (
        "import json,os,sys;s=os.stat(sys.argv[1]);"
        "print(json.dumps([s.st_uid,s.st_gid,oct(s.st_mode&0o7777),sorted(os.listdir(sys.argv[1]))]))"
    )
    admin_top = json.loads(
        in_container(
            stack, [SOCKET_DIR], user="10002:10002", mount=f"{admin}:{SOCKET_DIR}", script=stat
        )
    )
    assert admin_top == [10002, 10002, "0o700", ["kanidmd.sock"]]
    data_top = json.loads(
        in_container(
            stack, ["/data"], user="10002:10002", mount=f"{stack.idp_volume}:/data", script=stat
        )
    )
    assert "kanidmd.sock" not in data_top[3] and "kanidm.db" in data_top[3]

    # A client container that mounts only the admin volume, read-only, reaches it as
    # the idp user; any other user, and root without capabilities, does not.
    ro = f"{admin}:{SOCKET_DIR}:ro"
    assert in_container(stack, [], user="10002:10002", mount=ro, script=SOCKET_PROBE) == "reached"
    for user in ("10001:10001", "10001:10002", "0:0", "0:10002"):
        got = in_container(stack, [], user=user, mount=ro, script=SOCKET_PROBE)
        assert got == "refused: cannot use the socket (EACCES)", (user, got)

    # Restarting idp (as `./install.sh` on upgrade does) keeps it healthy with the socket.
    stack.compose("restart", "idp")
    start_idp_only(stack)
    rw = f"{admin}:{SOCKET_DIR}"
    assert in_container(stack, [], user="10002:10002", mount=rw, script=SOCKET_PROBE) == "reached"


def test_client_additions_against_the_real_idp(make_stack: MakeStack) -> None:
    stack = make_stack(idp=True)
    start_idp_only(stack)
    line = in_container(
        stack,
        [],
        user="10002:10002",
        mount=f"{stack.idp_admin_volume}:{SOCKET_DIR}:ro",
        script=ROUND_TRIP,
    )
    out = json.loads(line)
    spn = out.pop("group_spn")
    assert spn == "probe_users@" + "id.test.invalid"
    assert out == {
        "group_by_uuid": "probe_users",
        "unknown_uuid": None,
        "client_uuid_refused": True,
        # A new client has neither attribute: PKCE required, ES256.
        "default_pkce_attr": None,
        "default_legacy_attr": None,
        "settings": ["https://probe.test.invalid/callback", "true", "false", "false"],
        "pkce_disabled": "true",
        "http_refused": True,
        "both_added": True,
        "https_gone_app_kept": True,
        "strict_after_remove": "true",
        "scope_maps": {spn: ["openid", "profile"]},
        "managed_by_is_uuid": True,
        "token_id_matches_list": True,
        "delete_1": True,
        "delete_2": False,
        "tokens_left": 0,
    }

    # idm_admin is disabled again: logging in with a fresh recovery would be needed.
    again = in_container(
        stack,
        [],
        user="10002:10002",
        mount=f"{stack.idp_admin_volume}:{SOCKET_DIR}",
        script=SOCKET_PROBE,
    )
    assert again == "reached"
