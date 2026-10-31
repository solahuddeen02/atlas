"""A throwaway copy of the deploy/ stack for one test (ADR-005 acceptance tests).

Each Stack has its own Compose project name, secrets and backups directories and
external Postgres volume, so tests never touch each other or a real deployment.
"""

from __future__ import annotations

import json
import secrets
import socket
import subprocess
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "deploy"
COMPOSE_FILE = DEPLOY / "docker-compose.yml"
TEST_OVERLAY = DEPLOY / "compose.test.yml"
FAILING_MIGRATION_OVERLAY = DEPLOY / "compose.test-failing-migration.yml"
IMAGE = "atlas:local"


def run(
    args: list[str], *, check: bool = True, timeout: int = 600
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    if check and result.returncode != 0:
        raise AssertionError(
            f"{' '.join(args)} exited {result.returncode}\n"
            f"--- stdout ---\n{result.stdout[-4000:]}\n--- stderr ---\n{result.stderr[-4000:]}"
        )
    return result


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@dataclass
class Stack:
    workdir: Path
    project: str = field(default_factory=lambda: f"atlastest{secrets.token_hex(4)}")
    overlays: list[Path] = field(default_factory=lambda: [TEST_OVERLAY])
    create_volume: bool = True
    port: int = field(default_factory=free_port)

    def __post_init__(self) -> None:
        self.secrets_dir = self.workdir / "secrets"
        self.backup_dir = self.workdir / "backups"
        self.volume = f"{self.project}_pgdata"
        self.env_file = self.workdir / "stack.env"
        self.env_file.write_text(
            "\n".join(
                [
                    "ATLAS_PUBLIC_URL=https://atlas.test.invalid",
                    f"ATLAS_SECRETS_DIR={self.secrets_dir}",
                    f"ATLAS_BACKUP_DIR={self.backup_dir}",
                    f"ATLAS_PGDATA_VOLUME={self.volume}",
                    "ATLAS_IMAGE_TAG=local",
                    f"ATLAS_TEST_API_PORT={self.port}",
                    "TZ=UTC",
                ]
            )
            + "\n"
        )
        # The API needs OIDC settings that atlas-init doesn't generate (Phase 5 /
        # TODO(spike-s)). Put them in app.env first; init only appends missing keys.
        atlas_dir = self.secrets_dir / "atlas"
        atlas_dir.mkdir(parents=True, mode=0o700)
        (atlas_dir / "app.env").write_text(
            "ATLAS_OIDC_ISSUER=https://id.test.invalid\n"
            f"ATLAS_OIDC_CLIENT_SECRET={secrets.token_urlsafe(24)}\n"
        )
        if self.create_volume:
            run(["docker", "volume", "create", self.volume])

    def compose(
        self, *args: str, check: bool = True, timeout: int = 600
    ) -> subprocess.CompletedProcess[str]:
        cmd = ["docker", "compose", "-p", self.project, "--env-file", str(self.env_file)]
        cmd += ["-f", str(COMPOSE_FILE)]
        for overlay in self.overlays:
            cmd += ["-f", str(overlay)]
        return run([*cmd, *args], check=check, timeout=timeout)

    def up(self, *, check: bool = True) -> subprocess.CompletedProcess[str]:
        return self.compose("up", "-d", "--no-build", check=check)

    def wait_healthy(self, timeout: float = 240) -> None:
        deadline = time.monotonic() + timeout
        last = ""
        while time.monotonic() < deadline:
            result = self.compose("ps", "--all", "--format", "json", check=False)
            states = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
            api = [s for s in states if s.get("Service") == "atlas-api"]
            if api and api[0].get("Health") == "healthy":
                return
            failed = [s for s in states if s.get("State") == "exited" and s.get("ExitCode", 0) != 0]
            if failed:
                raise AssertionError(f"a service failed: {failed}\n{self.logs()}")
            last = json.dumps(states)[-2000:]
            time.sleep(2)
        raise AssertionError(f"atlas-api not healthy after {timeout}s: {last}\n{self.logs()}")

    def logs(self, *services: str) -> str:
        result = self.compose("logs", "--no-color", *services, check=False)
        return (result.stdout + result.stderr)[-8000:]

    def get(self, path: str) -> tuple[int, bytes]:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def psql(self, sql: str) -> str:
        # The postgres image trusts local socket connections inside the container.
        result = self.compose(
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "postgres",
            "-d",
            "atlas",
            "-tA",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            sql,
        )
        return result.stdout.strip()

    def destroy(self) -> None:
        self.compose("--profile", "tools", "down", "--remove-orphans", check=False)
        run(["docker", "volume", "rm", "-f", self.volume], check=False)
        # Files under secrets/ and backups/ belong to container uids now; remove
        # them as root inside a container so pytest's tmp cleanup doesn't fail.
        run(
            [
                "docker",
                "run",
                "--rm",
                "--user",
                "0:0",
                "--network",
                "none",
                "--entrypoint",
                "rm",
                "-v",
                f"{self.workdir}:/w",
                IMAGE,
                "-rf",
                "/w/secrets",
                "/w/backups",
            ],
            check=False,
        )
