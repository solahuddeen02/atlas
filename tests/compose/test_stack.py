"""Compose acceptance tests for Phase 3 (PLAN-v1; ADR-004 §3, ADR-005 §3, §4).

The proxy (Phase 4) has its own file, test_proxy.py.

Run by the `compose` CI job (RUN_COMPOSE_TESTS=1). Each test builds its own
throwaway stack; see tests/compose/stack.py.
"""

import json
import re
from collections.abc import Callable

from tests.compose.stack import (
    COMPOSE_FILE,
    DEPLOY,
    FAILING_MIGRATION_OVERLAY,
    Stack,
    run,
)

MakeStack = Callable[..., Stack]


def test_compose_config_renders_from_env_example() -> None:
    # Carried over from Phase 2: the template renders as-is.
    result = run(
        [
            "docker",
            "compose",
            "--env-file",
            str(DEPLOY / ".env.example"),
            "-f",
            str(COMPOSE_FILE),
            "config",
            "--quiet",
        ]
    )
    assert result.returncode == 0


def test_stack_comes_up_healthy(make_stack: MakeStack) -> None:
    stack = make_stack()
    stack.up()
    stack.wait_healthy()
    assert stack.get("/health") == (200, b'{"status":"ok"}')
    # Production mode: no interactive docs (through the proxy).
    assert stack.get("/docs")[0] == 404
    assert stack.get("/openapi.json")[0] == 404
    assert stack.psql("SELECT id FROM kernel.modules") == "kernel"


def test_secret_file_permissions(make_stack: MakeStack) -> None:
    stack = make_stack()
    stack.up()
    stack.wait_healthy()
    owners = json.loads(
        stack.compose(
            "run",
            "--rm",
            "--no-deps",
            "--user",
            "0:0",
            "--entrypoint",
            "python",
            "atlas-init",
            "-c",
            "import json,os;print(json.dumps({p:[os.stat(p).st_uid,os.stat(p).st_gid,"
            "oct(os.stat(p).st_mode&0o777)] for p in ['/secrets/atlas','/secrets/atlas/app.env',"
            "'/secrets/postgres','/secrets/postgres/password','/backups']}))",
        )
        .stdout.strip()
        .splitlines()[-1]
    )
    assert owners == {
        "/secrets/atlas": [10001, 10001, "0o700"],
        "/secrets/atlas/app.env": [10001, 10001, "0o600"],
        "/secrets/postgres": [999, 10001, "0o750"],
        "/secrets/postgres/password": [999, 10001, "0o640"],
        "/backups": [10001, 10001, "0o700"],
    }
    # The API container doesn't have the Postgres secrets at all.
    probe = stack.compose(
        "exec", "-T", "atlas-api", "test", "-e", "/run/secrets/postgres", check=False
    )
    assert probe.returncode == 1
    assert stack.compose("exec", "-T", "atlas-api", "id", "-u").stdout.strip() == "10001"


def test_down_v_preserves_data(make_stack: MakeStack) -> None:
    stack = make_stack()
    stack.up()
    stack.wait_healthy()
    stack.psql("INSERT INTO kernel.settings (key, value) VALUES ('marker', '\"kept\"')")

    stack.compose("down", "-v")  # the command people run by accident
    stack.up()
    stack.wait_healthy()
    assert stack.psql("SELECT value #>> '{}' FROM kernel.settings WHERE key = 'marker'") == "kept"


def test_failed_migration_blocks_api(make_stack: MakeStack, failing_migration_image: str) -> None:
    stack = make_stack(overlays=[FAILING_MIGRATION_OVERLAY])
    result = stack.up(check=False)
    assert result.returncode != 0
    assert "atlas-migrate" in result.stderr + result.stdout

    assert "injected failure for test_failed_migration_blocks_api" in stack.logs("atlas-migrate")
    running = stack.compose("ps", "--status", "running", "--services").stdout.split()
    assert "atlas-api" not in running
    # The failed upgrade rolled back as a whole: no half-migrated schema.
    assert stack.psql("SELECT to_regclass('kernel.users') IS NULL") == "t"


def test_missing_volume_fails_loudly(make_stack: MakeStack) -> None:
    stack = make_stack(create_volume=False)
    result = stack.up(check=False)
    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert stack.volume in output
    assert re.search(r"external|not found|could not be found", output, re.IGNORECASE)
    # Nothing came up on an empty database.
    running = stack.compose("ps", "--status", "running", "--services").stdout.split()
    assert "postgres" not in running
    assert "atlas-api" not in running


def test_backup_restore_roundtrip(make_stack: MakeStack) -> None:
    source = make_stack("source")
    source.up()
    source.wait_healthy()
    source.psql("INSERT INTO kernel.settings (key, value) VALUES ('marker', '\"from-source\"')")
    out = source.compose("--profile", "tools", "run", "--rm", "atlas-backup").stdout
    match = re.search(r"backup written: /var/backups/atlas/(atlas-\d{8}T\d{6}Z)", out)
    assert match, out
    name = match.group(1)

    # A scratch stack with its own secrets and volume, sharing the backups dir.
    scratch = make_stack("scratch")
    scratch.env_file.write_text(
        scratch.env_file.read_text().replace(
            f"ATLAS_BACKUP_DIR={scratch.backup_dir}", f"ATLAS_BACKUP_DIR={source.backup_dir}"
        )
    )
    scratch.up()
    scratch.wait_healthy()
    assert scratch.psql("SELECT count(*) FROM kernel.settings WHERE key = 'marker'") == "0"

    scratch.compose("stop", "atlas-api")
    scratch.compose(
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
    scratch.compose("start", "atlas-api")
    scratch.wait_healthy()

    assert (
        scratch.psql("SELECT value #>> '{}' FROM kernel.settings WHERE key = 'marker'")
        == "from-source"
    )
    # The append-only audit_log grant survived the restore.
    assert (
        scratch.psql(
            "SELECT has_table_privilege('atlas_app', 'kernel.audit_log', 'UPDATE'), "
            "has_table_privilege('atlas_app', 'kernel.audit_log', 'INSERT')"
        )
        == "f|t"
    )
    assert scratch.get("/health") == (200, b'{"status":"ok"}')
