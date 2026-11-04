"""Compose acceptance tests for Phase 5a: the bundled Kanidm IdP (PLAN-v1; ADR-001).

Run by the `compose` CI job (RUN_COMPOSE_TESTS=1). Each test builds its own
throwaway stack; see tests/compose/stack.py.
"""

import http.client
import json
import re
import secrets
import time
from collections.abc import Callable

import pytest

from tests.compose.stack import IDP_HOST, Stack, run

MakeStack = Callable[..., Stack]

SPOOFED = "203.0.113.77"  # TEST-NET-3: never a real peer here
IDP_IMAGE = "kanidm/server@sha256:71b6ff5a9c9c32b632894d8acef5a2afc77941b35a0aa95dcee959fe688d5ce9"
FAST_BACKUPS = "*/5 * * * * * *"  # every 5 s (Kanidm's 7-field form: sec ... year)
CONFIG = "/etc/atlas-idp/server.toml"
BACKUP_WAIT = 90


def rendered(stack: Stack) -> dict[str, dict[str, dict[str, object]]]:
    return json.loads(stack.compose("config", "--format", "json").stdout)


def kanidm_clients(stack: Stack, probe: str) -> list[str]:
    """The client addresses Kanidm logged for requests whose URI contains `probe`."""
    out = stack.compose("logs", "--no-color", "--no-log-prefix", "idp", check=False).stdout
    found = []
    for line in out.splitlines():
        if probe in line:
            found += re.findall(r"client_address\W{1,4}([0-9A-Fa-f:.]+)", line)
    return found


def wait_for_kanidm_log(stack: Stack, probe: str) -> list[str]:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if clients := kanidm_clients(stack, probe):
            return clients
        time.sleep(1)
    stack.fail(f"Kanidm logged no client_address for a request with {probe!r}")
    return []


# The newest Kanidm online backup, read inside an atlas container with the backups dir.
NEWEST_ONLINE = (
    "import json;from pathlib import Path;"
    "from atlas.kernel.db.backup import newest_kanidm_backup,read_kanidm_backup_header;"
    "d=Path('/var/backups/atlas/idp-online');p=newest_kanidm_backup(d);"
    "print(json.dumps(None if p is None else [p.name,*read_kanidm_backup_header(p)]))"
)


def newest_online(stack: Stack) -> list[str] | None:
    result: list[str] | None = json.loads(stack.python_in("atlas-backup", NEWEST_ONLINE))
    return result


def wait_for_online_backup(stack: Stack, *, other_than: str | None = None) -> list[str]:
    """Wait until Kanidm has written an online backup (newer than `other_than`)."""
    deadline = time.monotonic() + BACKUP_WAIT
    while time.monotonic() < deadline:
        newest = newest_online(stack)
        if newest is not None and newest[0] != other_than:
            return newest
        time.sleep(3)
    stack.fail(f"Kanidm wrote no new online backup within {BACKUP_WAIT}s")
    return []


def test_bundled_config(make_stack: MakeStack) -> None:
    stack = make_stack(idp=True, create_volume=False)
    config = rendered(stack)
    services = config["services"]
    assert {"atlas-idp-init", "idp"} <= set(services)
    idp = services["idp"]
    assert idp["image"] == IDP_IMAGE
    assert idp["user"] == "10002:10002"  # never root
    assert idp["read_only"] is True
    assert "ports" not in idp
    assert set(idp["networks"]) == {"idp-front", "idp-back"}
    assert set(services["atlas-proxy"]["networks"]) == {"default", "idp-front"}
    assert set(services["atlas-api"]["networks"]) == {"default", "idp-back"}
    healthcheck = idp["healthcheck"]["test"]
    assert "healthcheck" in healthcheck and "-v" in healthcheck
    networks = config["networks"]
    assert networks["idp-front"]["internal"] is True
    assert networks["idp-back"]["internal"] is True
    assert networks["idp-front"]["ipam"] == {"config": [{"subnet": stack.idp_subnet}]}
    depends = services["atlas-proxy"]["depends_on"]["idp"]
    assert depends["required"] is False and depends["condition"] == "service_healthy"
    # Kanidm's config comes only from server.toml (ADR-001): no KANIDM_* anywhere.
    assert "KANIDM_" not in stack.compose("config").stdout


def test_idp_healthy_and_proxied_over_verified_tls(make_stack: MakeStack) -> None:
    stack = make_stack(idp=True)  # the default (hourly) online-backup schedule
    stack.up()
    stack.wait_healthy()

    info = json.loads(run(["docker", "inspect", stack.container_id("idp")]).stdout)[0]
    assert info["Config"]["User"] == "10002:10002"
    assert info["State"]["Health"]["Status"] == "healthy"
    assert "-v" in info["Config"]["Healthcheck"]["Test"]
    # The verifying healthcheck, run once more by hand.
    check = stack.compose(
        "exec",
        "-T",
        "idp",
        "/sbin/kanidmd",
        "scripting",
        "healthcheck",
        "-v",
        "-c",
        CONFIG,
        check=False,
    )
    assert check.returncode == 0, check.stdout + check.stderr

    # Through atlas-proxy, which re-encrypts with proxy_ssl_verify on.
    status, _, body = stack.request("/status", host=IDP_HOST)
    assert (status, body.strip()) == (200, b"true")
    entries = [e for e in stack.proxy_log() if e.get("host") == IDP_HOST]
    assert entries and entries[-1]["status"] == 200
    # Unknown hosts are still dropped.
    with pytest.raises((http.client.RemoteDisconnected, ConnectionResetError)):
        stack.request("/status", host="evil.example.net")

    logs = stack.logs("idp")
    assert "Online backup schedule parsed as" in logs  # the default schedule parses
    assert "running as uid == 0" not in logs


def test_idp_files_and_dirs_ownership(make_stack: MakeStack) -> None:
    stack = make_stack(idp=True)
    stack.up()
    stack.wait_healthy()
    paths = [
        "/secrets/idp-ca",
        "/secrets/idp-ca/ca.key",
        "/secrets/idp",
        "/secrets/idp/server.key",
        "/secrets/idp/chain.pem",
        "/secrets/idp/server.toml",
        "/secrets/idp-trust",
        "/secrets/idp-trust/ca.pem",
        "/backups/idp-online",
        "/backups/idp-restore",
    ]
    script = (
        "import json,os,sys;print(json.dumps({p:[os.stat(p).st_uid,os.stat(p).st_gid,"
        "oct(os.stat(p).st_mode&0o7777)] for p in sys.argv[1:]}))"
    )
    result = stack.compose(
        "run",
        "--rm",
        "--no-deps",
        "--user",
        "0:0",
        "--entrypoint",
        "python",
        "atlas-init",
        "-c",
        script,
        *paths,
    )
    owners = json.loads(result.stdout.strip().splitlines()[-1])
    assert owners == {
        "/secrets/idp-ca": [0, 0, "0o700"],
        "/secrets/idp-ca/ca.key": [0, 0, "0o600"],
        "/secrets/idp": [0, 10002, "0o750"],
        "/secrets/idp/server.key": [0, 10002, "0o440"],
        "/secrets/idp/chain.pem": [0, 10002, "0o440"],
        "/secrets/idp/server.toml": [0, 10002, "0o440"],
        "/secrets/idp-trust": [0, 0, "0o755"],
        "/secrets/idp-trust/ca.pem": [0, 0, "0o644"],
        "/backups/idp-online": [10002, 10001, "0o2750"],
        "/backups/idp-restore": [10001, 10002, "0o2750"],
    }
    # The CA private key is mounted into no running service.
    for service in ("atlas-proxy", "atlas-api"):
        mounts = run_inspect_mounts(stack, service)
        assert not [m for m in mounts if "idp-ca" in m], (service, mounts)
    idp_mounts = run_inspect_mounts(stack, "idp")
    assert not [m for m in idp_mounts if "idp-ca" in m or m.endswith("/secrets")]


def run_inspect_mounts(stack: Stack, service: str) -> list[str]:
    info = json.loads(run(["docker", "inspect", stack.container_id(service)]).stdout)[0]
    return [m["Source"] for m in info["Mounts"] if m.get("Source")]


@pytest.mark.parametrize("idp", [False, True], ids=["byo", "bundled"])
def test_proxy_gateway_trust_unchanged_with_idp_network(make_stack: MakeStack, idp: bool) -> None:
    # ADR-003 §1: with idp-front attached (in both modes), the trusted gateway is still
    # the default network's: idp-front is internal and has no route.
    stack = make_stack(idp=idp)
    stack.up()
    stack.wait_healthy()
    line = next(line for line in stack.logs("atlas-proxy").splitlines() if "trusted peers:" in line)
    peers = line.split("trusted peers:")[1].split()
    gateway = stack.network_gateway("default")
    assert peers == ["127.0.0.1/32", "::1/128", f"{gateway}/32"]
    assert not gateway.startswith(stack.idp_subnet.rsplit(".", 1)[0] + ".")


def test_kanidm_sees_the_real_client_ip(make_stack: MakeStack) -> None:
    stack = make_stack(idp=True)
    stack.up()
    stack.wait_healthy()

    # 1. From the host: the peer is the trusted gateway (where a tunnel connects), so
    #    its X-Forwarded-For names the client, and Kanidm sees that client.
    probe = "probe" + secrets.token_hex(4)
    stack.request(f"/ui/login?{probe}", host=IDP_HOST, headers={"X-Forwarded-For": SPOOFED})
    assert wait_for_kanidm_log(stack, probe) == [SPOOFED]

    # 2. From an untrusted container: atlas-proxy ignores its X-Forwarded-For and
    #    overwrites the header with the real peer, which is what Kanidm logs.
    probe = "probe" + secrets.token_hex(4)
    stack.from_inside(f"/ui/login?{probe}", host=IDP_HOST, headers={"X-Forwarded-For": SPOOFED})
    seen = wait_for_kanidm_log(stack, probe)
    assert seen and SPOOFED not in seen

    # 3. Straight to Kanidm over idp-back (verified TLS, internal CA): that network is
    #    not trusted, so Kanidm ignores a forged header and logs the connection address.
    probe = "probe" + secrets.token_hex(4)
    script = (
        "import http.client,ssl,sys;"
        "ctx=ssl.create_default_context(cafile='/ca/ca.pem');"
        "c=http.client.HTTPSConnection('idp',8443,context=ctx,timeout=10);"
        "c.request('GET',sys.argv[1],headers={'Host':sys.argv[2],'X-Forwarded-For':sys.argv[3]});"
        "print(c.getresponse().status)"
    )
    status = stack.python_in(
        "atlas-api",
        script,
        f"/ui/login?{probe}",
        IDP_HOST,
        SPOOFED,
        mounts=(f"{stack.secrets_dir}/idp-trust:/ca:ro",),
    )
    assert status.isdigit()
    seen = wait_for_kanidm_log(stack, probe)
    assert seen and SPOOFED not in seen


def test_idp_backup_restore_roundtrip(make_stack: MakeStack) -> None:
    # Source-unique signal: the Kanidm domain UUID (db_d_uuid), generated by each
    # instance on its first start and carried in its backups.
    source = make_stack("source", idp=True, idp_backup_schedule=FAST_BACKUPS)
    source.up()
    source.wait_healthy()
    source.psql("INSERT INTO kernel.settings (key, value) VALUES ('marker', '\"from-source\"')")
    wait_for_online_backup(source)

    out = source.compose("--profile", "tools", "run", "--rm", "atlas-backup").stdout
    match = re.search(r"backup written: /var/backups/atlas/(atlas-\d{8}T\d{6}Z)", out)
    assert match, out
    name = match.group(1)
    script = (
        "import json,os,sys;d=sys.argv[1];m=json.load(open(d+'/manifest.json'));"
        "print(json.dumps([m,oct(os.stat(d+'/idp.json.gz').st_mode&0o777)]))"
    )
    manifest, idp_mode = json.loads(
        source.python_in("atlas-backup", script, f"/var/backups/atlas/{name}")
    )
    idp = manifest["idp"]
    assert idp_mode == "0o600"
    assert idp["file"] == "idp.json.gz" and idp["kanidm_version"] == "1.12"
    assert 0 <= idp["age_seconds"] < BACKUP_WAIT
    source_domain = idp["domain_uuid"]

    # No more source backups into the shared dir from here on.
    source.compose("stop", "idp")
    last_source = newest_online(source)
    assert last_source is not None

    scratch = make_stack("scratch", idp=True, idp_backup_schedule=FAST_BACKUPS)
    scratch.env_file.write_text(
        scratch.env_file.read_text().replace(
            f"ATLAS_BACKUP_DIR={scratch.backup_dir}", f"ATLAS_BACKUP_DIR={source.backup_dir}"
        )
    )
    scratch.up()
    scratch.wait_healthy()
    fresh = wait_for_online_backup(scratch, other_than=last_source[0])
    assert fresh[2] != source_domain  # a new instance has its own domain UUID

    scratch.compose("stop", "atlas-api")
    restore = scratch.compose(
        "--profile",
        "tools",
        "run",
        "--rm",
        "atlas-backup",
        "atlas-admin",
        "restore",
        "--from",
        f"/var/backups/atlas/{name}",
        "--yes",
    )
    steps = [line.strip() for line in restore.stdout.splitlines() if line.startswith("  docker")]
    assert steps[0] == "docker compose stop idp", restore.stdout
    assert steps[2] == "docker compose start idp"
    staged = re.search(r"/restore/(\S+\.json\.gz)", steps[1])
    assert staged and steps[1] == (
        "docker compose run --rm --no-deps idp /sbin/kanidmd database restore "
        f"-c {CONFIG} /restore/{staged.group(1)}"
    )
    # The printed steps, run as printed (minus the `docker compose` prefix).
    for step in steps:
        args = step.split()[2:]
        result = scratch.compose(*args, check=False, timeout=180)
        assert result.returncode == 0, (step, result.stdout[-3000:], result.stderr[-3000:])
    scratch.compose("start", "atlas-api")
    scratch.wait_healthy()

    restored = wait_for_online_backup(scratch, other_than=fresh[0])
    assert restored[2] == source_domain  # the restored IdP holds the source's data
    assert (
        scratch.psql("SELECT value #>> '{}' FROM kernel.settings WHERE key = 'marker'")
        == "from-source"
    )
    status, _, body = scratch.request("/status", host=IDP_HOST)
    assert (status, body.strip()) == (200, b"true")


def test_restore_refuses_another_kanidm_version(make_stack: MakeStack) -> None:
    stack = make_stack(idp=True, idp_backup_schedule=FAST_BACKUPS)
    stack.up()
    stack.wait_healthy()
    wait_for_online_backup(stack)
    out = stack.compose("--profile", "tools", "run", "--rm", "atlas-backup").stdout
    match = re.search(r"backup written: /var/backups/atlas/(atlas-\d{8}T\d{6}Z)", out)
    assert match, out
    stack.compose("stop", "atlas-api")
    result = stack.compose(
        "--profile",
        "tools",
        "run",
        "--rm",
        "-e",
        "ATLAS_IDP_KANIDM_SERIES=1.11",
        "atlas-backup",
        "atlas-admin",
        "restore",
        "--from",
        f"/var/backups/atlas/{match.group(1)}",
        "--yes",
        check=False,
    )
    assert result.returncode == 1
    assert "this stack runs Kanidm 1.11" in result.stderr
    assert "Nothing was changed" in result.stderr
