"""Compose acceptance tests for Phase 5b-2b: the IdP bootstrap against the real Kanidm.

Run by the `compose` CI job (RUN_COMPOSE_TESTS=1). The full stack comes up with no
OIDC setting written by hand: the one-shot service atlas-idp-bootstrap creates the
client, the service account, the role groups and the files atlas-api needs.
"""

import json
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from tests.compose.stack import IMAGE, Stack, run

MakeStack = Callable[..., Stack]

IMMICH_URL = "https://immich.test.invalid"
IMMICH_REDIRECTS = [
    f"{IMMICH_URL}/auth/login",
    f"{IMMICH_URL}/user-settings",
    "app.immich:///oauth-callback",
]
ROLE_POLICY = {
    "atlas_role_owner": ["mfa", "28800"],
    "atlas_role_admin": ["mfa", "28800"],
    "atlas_role_adult": ["mfa", None],
    "atlas_role_child": ["any", None],
    "atlas_role_guest": ["any", None],
}
BOOTSTRAP_COMMAND = ["run", "--rm", "--no-deps", "-T", "atlas-idp-bootstrap"]
PREFIX = "atlas-admin idp-bootstrap: "
HAND_SET = "set-by-hand-before-the-bootstrap"

# --- helpers --------------------------------------------------------------------------

ADMIN_PRELUDE = """
import asyncio, json
from pathlib import Path
from pydantic import SecretStr
from atlas.kernel.identity.kanidm import AdminSocket, KanidmClient, KanidmError

SOCKET = AdminSocket(Path("/run/atlas-idp-admin/kanidmd.sock"))

async def with_admin(body):
    # The same way the bootstrap gets in; idm_admin is disabled again afterwards.
    password = await SOCKET.recover_account("idm_admin")
    try:
        async with KanidmClient(base_url="https://idp:8443", ca_file=Path("/ca/ca.pem")) as client:
            await client.login("idm_admin", password)
            return await body(client)
    finally:
        await SOCKET.disable_account("idm_admin")
"""


def as_idm_admin(stack: Stack, body: str) -> object:
    """Run `body` (the text of `async def body(client)`) as idm_admin in a one-off container
    on idp-back, as the idp user. It prints one JSON line, and never a secret."""
    script = f"{ADMIN_PRELUDE}\n{body}\nprint(json.dumps(asyncio.run(with_admin(body))))"
    result = run(
        [
            "docker", "run", "--rm", "--user", "10002:10002",
            "--network", f"{stack.project}_idp-back",
            "-v", f"{stack.idp_admin_volume}:/run/atlas-idp-admin:ro",
            "-v", f"{stack.secrets_dir}/idp-trust:/ca:ro",
            "-v", f"{stack.secrets_dir}/atlas-idp:/state:ro",
            "--entrypoint", "python", IMAGE, "-c", script,
        ],
        check=False,
    )  # fmt: skip
    if result.returncode != 0:
        stack.fail(f"the idm_admin script exited {result.returncode}\n{result.stderr[-3000:]}")
    return json.loads(result.stdout.strip().splitlines()[-1])


def as_root(stack: Stack, script: str) -> object:
    """Run a Python script as root with the secrets dir read-only at /s; it prints JSON."""
    result = run(
        [
            "docker", "run", "--rm", "--user", "0:0", "--network", "none",
            "-v", f"{stack.secrets_dir}:/s:ro",
            "--entrypoint", "python", IMAGE, "-c", script,
        ],
        check=False,
    )  # fmt: skip
    if result.returncode != 0:
        stack.fail(f"the root script exited {result.returncode}\n{result.stderr[-3000:]}")
    return json.loads(result.stdout.strip().splitlines()[-1])


FILES_SCRIPT = """
import json, os, stat
out = {}
for d in ("/s/atlas-idp", "/s/idp-clients"):
    st = os.stat(d)
    out[d[3:]] = [st.st_uid, st.st_gid, oct(stat.S_IMODE(st.st_mode))]
    for name in sorted(os.listdir(d)):
        st = os.stat(os.path.join(d, name))
        out[d[3:] + "/" + name] = [st.st_uid, st.st_gid, oct(stat.S_IMODE(st.st_mode))]
print(json.dumps(out))
"""

RECORD_SCRIPT = """
import json
print(json.dumps({"complete": json.load(open("/s/atlas-idp/bootstrap.json"))["complete"]}))
"""

FINGERPRINT_SCRIPT = """
import hashlib, json, os
out = {}
for d in ("/s/atlas-idp", "/s/idp-clients"):
    for name in sorted(os.listdir(d)):
        path = os.path.join(d, name)
        out[d[3:] + "/" + name] = [hashlib.sha256(open(path, "rb").read()).hexdigest(),
                                   os.stat(path).st_mtime_ns]
print(json.dumps(out))
"""

# What is secret in the two files that hold secrets: every value, and each part of a token.
SECRETS_SCRIPT = """
import json
from dotenv import dotenv_values
found = []
for path in ("/s/atlas-idp/idp.env", "/s/idp-clients/immich.env"):
    for value in dotenv_values(path).values():
        if value:
            found += [value, *value.split(".")]
print(json.dumps([s for s in found if len(s) >= 12]))
"""

STATE_SCRIPT = f"""
async def body(client):
    out = {{"groups": {{}}, "clients": {{}}}}
    for name in {json.dumps(list(ROLE_POLICY))}:
        entry = await client.get_group(name)
        out["groups"][name] = [entry.first("credential_type_minimum"),
                               entry.first("authsession_expiry")]
    for name in ("idm_all_persons", "atlas_users"):
        entry = await client.get_group(name)
        out["groups"][name] = [entry.first("credential_type_minimum"), sorted(entry.attrs.get("member", ()))]
    for name in ("atlas", "immich"):
        entry = await client.get_oauth2_client(name)
        if entry is None:
            out["clients"][name] = None
            continue
        out["clients"][name] = {{
            "landing": entry.first("oauth2_rs_origin_landing"),
            "origins": sorted(entry.attrs.get("oauth2_rs_origin", ())),
            "strict": entry.first("oauth2_strict_redirect_uri"),
            "legacy": entry.first("oauth2_jwt_legacy_crypto_enable"),
            "no_pkce": entry.first("oauth2_allow_insecure_client_disable_pkce"),
            "scopes": {{k: sorted(v) for k, v in entry.scope_maps.items()}},
        }}
    account = await client.get_service_account("atlas-console")
    out["console"] = account is not None
    tokens = await client.list_api_tokens("atlas-console")
    out["tokens"] = [[t.token_id, t.label] for t in tokens]
    return out
"""


def idp_state(stack: Stack) -> dict[str, dict[str, object]]:
    state = as_idm_admin(stack, STATE_SCRIPT)
    assert isinstance(state, dict)
    return state


def run_bootstrap(stack: Stack, *flags: str) -> tuple[int, list[str]]:
    """A one-off atlas-idp-bootstrap with `flags`: (exit status, its printed lines)."""
    command = ["atlas-admin", "idp-bootstrap", *flags] if flags else []
    result = stack.compose(*BOOTSTRAP_COMMAND, *command, check=False, timeout=120)
    # Only the command's own lines: Compose prints "Container ... Creating" around it.
    lines = (result.stdout + result.stderr).splitlines()
    return result.returncode, [ln for ln in lines if ln.startswith(PREFIX)]


def bootstrap_lines(stack: Stack) -> list[str]:
    """What the service printed on its start under `docker compose up`, without the prefix."""
    out = stack.compose("logs", "--no-color", "--no-log-prefix", "atlas-idp-bootstrap").stdout
    return [line for line in out.splitlines() if line.strip()]


def full_log(stack: Stack, service: str) -> str:
    result = stack.compose("logs", "--no-color", "--no-log-prefix", service, check=False)
    return result.stdout + result.stderr


def idm_admin_is_disabled(stack: Stack) -> bool:
    """A login with a wrong password: Kanidm says `account_expired` only for a disabled account
    (an enabled one says `incorrect_password`)."""
    script = (
        "import asyncio, json\n"
        "from pathlib import Path\n"
        "from pydantic import SecretStr\n"
        "from atlas.kernel.identity.kanidm import KanidmAuthError, KanidmClient\n"
        "async def main():\n"
        "    async with KanidmClient(base_url='https://idp:8443', ca_file=Path('/ca/ca.pem')) as c:\n"
        "        try:\n"
        "            await c.login('idm_admin', SecretStr('definitely-not-the-password-1'))\n"
        "        except KanidmAuthError as exc:\n"
        "            return exc.reason\n"
        "print(json.dumps(asyncio.run(main())))\n"
    )
    result = run(
        [
            "docker", "run", "--rm", "--user", "10002:10002",
            "--network", f"{stack.project}_idp-back",
            "-v", f"{stack.secrets_dir}/idp-trust:/ca:ro",
            "--entrypoint", "python", IMAGE, "-c", script,
        ],
        check=False,
    )  # fmt: skip
    assert result.returncode == 0, result.stderr[-2000:]
    reason = json.loads(result.stdout.strip().splitlines()[-1])
    assert reason in ("account_expired", "incorrect_password"), reason
    return reason == "account_expired"


def not_secret_in(stack: Stack, secrets_found: list[str], services: tuple[str, ...]) -> None:
    leaks = [
        service
        for service in services
        if any(secret in full_log(stack, service) for secret in secrets_found)
    ]
    assert not leaks, f"a stored secret appears in the log of: {leaks}"


def api_view_script() -> str:
    return (
        "import json\n"
        "from dotenv import dotenv_values\n"
        "from atlas.kernel.config import load_settings\n"
        "s = load_settings()\n"
        "idp = dotenv_values('/run/secrets/atlas-idp/idp.env')\n"
        "app = dotenv_values('/run/secrets/atlas/app.env')\n"
        "secret = s.oidc_client_secret.get_secret_value()\n"
        "print(json.dumps({\n"
        "  'from_idp_env': secret == idp['ATLAS_OIDC_CLIENT_SECRET'],\n"
        "  'hand_set_overridden': app.get('ATLAS_OIDC_CLIENT_SECRET') not in (None, secret),\n"
        "  'token_from_idp_env': s.idp_admin_token is not None\n"
        "      and s.idp_admin_token.get_secret_value() == idp['ATLAS_IDP_ADMIN_TOKEN'],\n"
        "  'issuer': s.oidc_issuer,\n"
        "}))\n"
    )


# --- the tests ------------------------------------------------------------------------


def test_the_bootstrap_sets_the_stack_up_and_is_idempotent(make_stack: MakeStack) -> None:
    stack = make_stack(idp=True, extra_env={"ATLAS_IMMICH_PUBLIC_URL": IMMICH_URL})
    # A client secret set by hand before the bootstrap existed: the generated one wins.
    atlas_dir = stack.secrets_dir / "atlas"
    (atlas_dir / "app.env").write_text(f"ATLAS_OIDC_CLIENT_SECRET={HAND_SET}\n")

    stack.up()
    stack.wait_healthy()  # atlas-api started from the bootstrap's secrets alone

    # --- what the service printed: names, UUIDs, created/kept/updated -----------------
    lines = bootstrap_lines(stack)
    printed = "\n".join(lines)
    for expected in (
        "atlas-admin idp-bootstrap: full run: no bootstrap record yet",
        "atlas-admin idp-bootstrap: account idm_admin recovered",
        "atlas-admin idp-bootstrap: client atlas created",
        "atlas-admin idp-bootstrap: client immich created",
        "atlas-admin idp-bootstrap: account idm_admin disabled",
        "atlas-admin idp-bootstrap: done",
    ):
        assert any(line.startswith(expected) for line in lines), (expected, printed)
    assert idm_admin_is_disabled(stack)

    # --- the files: owners and modes ----------------------------------------------------
    assert as_root(stack, FILES_SCRIPT) == {
        "atlas-idp": [10002, 10001, "0o2750"],
        "atlas-idp/idp.env": [10002, 10001, "0o640"],
        "atlas-idp/groups.json": [10002, 10001, "0o640"],
        "atlas-idp/bootstrap.json": [10002, 10001, "0o640"],
        "idp-clients": [10002, 10002, "0o700"],
        "idp-clients/immich.env": [10002, 10002, "0o600"],
    }

    # --- atlas-api reads them, and the value set by hand is overridden --------------------
    view = stack.compose("exec", "-T", "atlas-api", "python", "-c", api_view_script())
    assert json.loads(view.stdout.strip().splitlines()[-1]) == {
        "from_idp_env": True,
        "hand_set_overridden": True,
        "token_from_idp_env": True,
        "issuer": "https://id.test.invalid/oauth2/openid/atlas",
    }

    # --- the IdP: role groups, policies, the lowered default, clients, one token ---------
    state = idp_state(stack)
    groups = state["groups"]
    assert isinstance(groups, dict)
    for name, policy in ROLE_POLICY.items():
        assert groups[name] == policy, name
    assert groups["idm_all_persons"][0] == "any"
    assert set(groups["atlas_users"][1]) == {f"{name}@id.test.invalid" for name in ROLE_POLICY}
    assert state["console"] is True
    tokens = state["tokens"]
    assert isinstance(tokens, list) and [t[1] for t in tokens] == ["atlas-console"]
    clients = state["clients"]
    assert isinstance(clients, dict)
    atlas, immich = clients["atlas"], clients["immich"]
    callback = "https://atlas.test.invalid/api/v1/auth/callback"
    assert atlas["landing"] == callback and atlas["origins"] == [callback]
    assert atlas["strict"] == "true" and atlas["legacy"] in (None, "false")
    assert atlas["no_pkce"] in (None, "false")
    assert atlas["scopes"] == {"atlas_users@id.test.invalid": ["groups", "openid", "profile"]}
    assert immich["landing"] == IMMICH_REDIRECTS[0]
    assert immich["origins"] == sorted(IMMICH_REDIRECTS)  # exactly three
    assert immich["strict"] == "true" and immich["legacy"] in (None, "false")
    assert immich["scopes"] == {"immich_users@id.test.invalid": ["email", "openid", "profile"]}

    # --- no stored secret in any log -----------------------------------------------------
    found = as_root(stack, SECRETS_SCRIPT)
    assert isinstance(found, list) and len(found) >= 6
    not_secret_in(stack, found, ("atlas-idp-bootstrap", "idp", "atlas-api", "atlas-proxy"))

    # --- a second run does nothing, and still sends DisableAccount -------------------------
    before = as_root(stack, FINGERPRINT_SCRIPT)
    code, second = run_bootstrap(stack)
    assert code == 0, second
    assert [line.removeprefix("atlas-admin idp-bootstrap: ") for line in second[-2:]] == [
        "up to date: nothing to change",
        "account idm_admin disabled",
    ]
    assert not any("recovered" in line for line in second)
    unchanged = as_root(stack, FINGERPRINT_SCRIPT)
    assert isinstance(before, dict) and isinstance(unchanged, dict)
    assert set(unchanged) == set(before)
    for name, (digest, mtime) in before.items():
        # Not a byte of any file changed. The record is rewritten (incomplete, then complete
        # again: the same bytes); no other file is touched, not even its mtime.
        assert unchanged[name][0] == digest, name
        if name != "atlas-idp/bootstrap.json":
            assert unchanged[name][1] == mtime, name
    assert idm_admin_is_disabled(stack)

    # --- --verify: a full run in which everything is kept ----------------------------------
    code, verified = run_bootstrap(stack, "--verify")
    assert code == 0, verified
    text = "\n".join(verified)
    assert "full run: requested" in text and "account idm_admin recovered" in text
    changed = [ln for ln in verified if " created" in ln or " updated" in ln or "removed" in ln]
    assert changed == [], changed
    after = as_root(stack, FINGERPRINT_SCRIPT)
    assert isinstance(before, dict) and isinstance(after, dict)
    for name in ("atlas-idp/idp.env", "atlas-idp/groups.json", "idp-clients/immich.env"):
        assert after[name] == before[name], name  # bytes and mtime: not rewritten
    assert idm_admin_is_disabled(stack)


def test_verify_repairs_what_was_changed_by_hand(make_stack: MakeStack) -> None:
    stack = make_stack(idp=True, extra_env={"ATLAS_IMMICH_PUBLIC_URL": IMMICH_URL})
    stack.up()
    stack.wait_healthy()

    # Someone adds a fourth redirect URL to Immich's client, a scope map for another group
    # to it, and loosens Atlas's client: none of it is visible to the fast path.
    tamper = """
async def body(client):
    await client.add_oauth2_redirect_url("immich", "https://immich.test.invalid/planted")
    await client.set_oauth2_scope_map("immich", "atlas_role_guest", ["openid"])
    await client.update_oauth2_client("atlas", strict_redirect_uri=False, legacy_crypto=True)
    refused = []
    for url in ("http://immich.test.invalid/auth/login", "https://", "javascript:alert(1)"):
        try:
            await client.add_oauth2_redirect_url("immich", url)
            refused.append(False)
        except ValueError:
            refused.append(True)
    return refused
"""
    assert as_idm_admin(stack, tamper) == [True, True, True]  # a fourth, insecure URL is refused
    planted = idp_state(stack)["clients"]
    assert isinstance(planted, dict)
    assert "https://immich.test.invalid/planted" in planted["immich"]["origins"]

    code, quick = run_bootstrap(stack)
    assert code == 0 and "up to date: nothing to change" in "\n".join(
        quick
    )  # the fast path is blind
    code, verified = run_bootstrap(stack, "--verify")
    assert code == 0, verified
    text = "\n".join(verified)
    assert "redirect removed https://immich.test.invalid/planted" in text
    assert "scope map removed atlas_role_guest@id.test.invalid" in text

    clients = idp_state(stack)["clients"]
    assert isinstance(clients, dict)
    assert clients["immich"]["origins"] == sorted(IMMICH_REDIRECTS)
    assert clients["immich"]["scopes"] == {
        "immich_users@id.test.invalid": ["email", "openid", "profile"]
    }
    assert clients["atlas"]["strict"] == "true"
    assert clients["atlas"]["legacy"] in (None, "false")


def failing_bootstrap(stack: Stack, tmp_path: Path) -> Path:
    """An overlay that points the bootstrap at a Kanidm that does not exist. The service
    still reaches the admin socket, so RecoverAccount succeeds; then it cannot log in, and
    the run fails."""
    overlay = tmp_path / "failing-bootstrap.yml"
    overlay.write_text(
        "services:\n"
        "  atlas-idp-bootstrap:\n"
        "    command: [atlas-admin, idp-bootstrap, '--idp-url=https://idp-nowhere:8443']\n"
    )
    return overlay


def assert_blocked(stack: Stack, up: subprocess.CompletedProcess[str]) -> None:
    """The three things a failed bootstrap must always cause, however it failed: `up` exits
    non-zero, atlas-api never becomes healthy, atlas-proxy is not started."""
    assert up.returncode != 0, "docker compose up succeeded although the bootstrap failed"
    # Not just at one instant: atlas-api gets several restarts to prove it cannot become healthy.
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        states = {str(s.get("Service")): s for s in stack.states()}
        assert states["atlas-api"].get("Health") != "healthy", "atlas-api became healthy"
        assert states.get("atlas-proxy", {}).get("State") != "running", "atlas-proxy started"
        time.sleep(3)
    states = {str(s.get("Service")): s for s in stack.states()}
    assert states["atlas-idp-bootstrap"].get("ExitCode") == 1
    # atlas-api says why and where to look.
    api = full_log(stack, "atlas-api")
    assert "refusing to start" in api and "docker compose logs atlas-idp-bootstrap" in api, api[
        -600:
    ]
    assert idm_admin_is_disabled(stack)  # disabled although the run failed


def repair(stack: Stack, timeout: float = 240) -> None:
    """The cause is gone: `docker compose up -d` (twice at most), and nothing else by hand.

    The first `up` runs the bootstrap in full and restarts atlas-api. Compose may report
    atlas-api as failed (exit 1) at that moment, because the container is still in Docker's
    restart backoff, and then leaves atlas-proxy unstarted. atlas-api finds the completed
    record and becomes healthy by itself, and a second `up` starts the proxy. (This is
    in docs/SELF_HOSTING.md. `Stack.wait_healthy` would call the restarting a crash loop,
    which it is until the repair, so this waits for the end of it itself.)
    """
    stack.overlays.clear()
    stack.up(check=False)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        api = [s for s in stack.states() if s.get("Service") == "atlas-api"]
        if api and api[0].get("Health") == "healthy":
            break
        time.sleep(3)
    else:
        stack.fail(f"atlas-api did not recover by itself within {timeout:.0f}s")
    stack.up()  # starts atlas-proxy, and returns 0
    stack.wait_healthy()


def test_a_first_run_that_fails_blocks_atlas_api_and_the_next_run_repairs(
    make_stack: MakeStack, tmp_path: Path
) -> None:
    stack = make_stack(idp=True)
    stack.overlays.append(failing_bootstrap(stack, tmp_path))

    assert_blocked(stack, stack.up(check=False))
    text = "\n".join(bootstrap_lines(stack))
    assert "failed at step login:" in text, text
    assert "account idm_admin recovered" in text and "account idm_admin disabled" in text
    record = as_root(stack, RECORD_SCRIPT)
    assert record == {"complete": False}

    # With the cause gone, the next `up` runs the bootstrap in full (see `repair`).
    repair(stack)
    repaired = "\n".join(bootstrap_lines(stack))
    assert "full run: the last run did not finish" in repaired, repaired
    assert "account idm_admin disabled" in repaired and "done" in repaired
    assert as_root(stack, RECORD_SCRIPT) == {"complete": True}
    assert idm_admin_is_disabled(stack)


def test_a_later_run_that_fails_blocks_atlas_api_too_with_the_old_files_on_disk(
    make_stack: MakeStack, tmp_path: Path
) -> None:
    stack = make_stack(idp=True)
    stack.up()
    stack.wait_healthy()  # a first run succeeded: idp.env, groups.json and the record exist
    files = as_root(stack, FILES_SCRIPT)
    assert isinstance(files, dict) and "atlas-idp/idp.env" in files
    assert as_root(stack, RECORD_SCRIPT) == {"complete": True}

    # The stack is stopped and started again (a reboot, an upgrade), and the bootstrap fails.
    stack.compose("down", "--timeout", "5", timeout=240)
    stack.overlays.append(failing_bootstrap(stack, tmp_path))
    assert_blocked(stack, stack.up(check=False))
    assert "failed at step login:" in "\n".join(bootstrap_lines(stack))
    # The old files are still there, and are exactly why the record is needed: without it
    # atlas-api would start on them, become healthy, and `up` would exit 0.
    assert as_root(stack, FILES_SCRIPT) == files
    assert as_root(stack, RECORD_SCRIPT) == {"complete": False}

    repair(stack)
    assert as_root(stack, RECORD_SCRIPT) == {"complete": True}
    assert idm_admin_is_disabled(stack)


def test_rotating_the_console_token_leaves_one_token_and_a_working_stored_one(
    make_stack: MakeStack,
) -> None:
    stack = make_stack(idp=True)
    stack.up()
    stack.wait_healthy()
    before = idp_state(stack)["tokens"]
    assert isinstance(before, list) and len(before) == 1
    old_id = before[0][0]

    code, rotated = run_bootstrap(stack, "--rotate-console-token")
    assert code == 0, rotated
    text = "\n".join(rotated)
    assert "restart atlas-api" in text  # tells the operator what to do next
    assert "token " in text and " created, replaced 1" in text

    # One token with our label, and it is a new one. (No waiting: Kanidm keeps accepting a
    # deleted token for up to 300 seconds, which the docs describe. The list is the proof.)
    after = idp_state(stack)["tokens"]
    assert isinstance(after, list)
    assert [t[1] for t in after] == ["atlas-console"] and after[0][0] != old_id

    # The stored token is the new one and Kanidm accepts it.
    script = (
        "import asyncio, json\n"
        "from pathlib import Path\n"
        "from dotenv import dotenv_values\n"
        "from pydantic import SecretStr\n"
        "from atlas.kernel.identity.kanidm import KanidmClient, api_token_id\n"
        "async def main():\n"
        "    token = SecretStr(dotenv_values('/state/idp.env')['ATLAS_IDP_ADMIN_TOKEN'])\n"
        "    async with KanidmClient(base_url='https://idp:8443', ca_file=Path('/ca/ca.pem'),\n"
        "                            token=token) as c:\n"
        "        entry = await c.get_group('atlas_users')\n"
        "    return [api_token_id(token), entry is not None]\n"
        "print(json.dumps(asyncio.run(main())))\n"
    )
    result = run(
        [
            "docker", "run", "--rm", "--user", "10002:10002",
            "--network", f"{stack.project}_idp-back",
            "-v", f"{stack.secrets_dir}/idp-trust:/ca:ro",
            "-v", f"{stack.secrets_dir}/atlas-idp:/state:ro",
            "--entrypoint", "python", IMAGE, "-c", script,
        ],
        check=False,
    )  # fmt: skip
    assert result.returncode == 0, result.stderr[-2000:]
    stored_id, works = json.loads(result.stdout.strip().splitlines()[-1])
    assert works is True and stored_id == after[0][0]

    # atlas-api picks it up after the restart the command asks for.
    stack.compose("restart", "atlas-api", timeout=120)
    stack.wait_healthy()
    assert idm_admin_is_disabled(stack)
