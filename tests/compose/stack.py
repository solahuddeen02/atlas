"""A throwaway copy of the deploy/ stack for one test (ADR-005 acceptance tests).

Each Stack has its own Compose project name, secrets and backups directories and
external Postgres volume, so tests never touch each other or a real deployment.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import secrets
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "deploy"
COMPOSE_FILE = DEPLOY / "docker-compose.yml"
FAILING_MIGRATION_OVERLAY = DEPLOY / "compose.test-failing-migration.yml"
IMAGE = "atlas:local"
PUBLIC_HOST = "atlas.test.invalid"

# Every wait is bounded. A hit limit fails the test with a clear message and the
# stack's state and logs (Stack.diagnostics), instead of hanging the CI job.
COMMAND_TIMEOUT = 90  # one docker/compose command
UP_TIMEOUT = 240  # `up -d` blocks until dependencies are healthy
DOWN_TIMEOUT = 240
HEALTHY_TIMEOUT = 180  # whole stack healthy after `up`
LOG_TIMEOUT = 90  # a service logging an expected line
DIAGNOSTIC_SERVICES = (
    "atlas-proxy",
    "atlas-api",
    "atlas-migrate",
    "atlas-db-init",
    "atlas-init",
    "postgres",
)

# Stacks alive right now, for the per-test watchdog (tests/compose/conftest.py).
ACTIVE: list[Stack] = []


class HarnessTimeout(AssertionError):
    pass


def run(
    args: list[str], *, check: bool = True, timeout: int = COMMAND_TIMEOUT
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raw = exc.stdout or ""
        out = raw if isinstance(raw, str) else raw.decode(errors="replace")
        raise HarnessTimeout(
            f"`{' '.join(args)}` did not finish within {timeout}s\n"
            f"--- partial output ---\n{out[-2000:]}"
        ) from None
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
    overlays: list[Path] = field(default_factory=list)
    extra_env: dict[str, str] = field(default_factory=dict)
    create_volume: bool = True
    port: int = field(default_factory=free_port)

    def __post_init__(self) -> None:
        ACTIVE.append(self)
        self.secrets_dir = self.workdir / "secrets"
        self.backup_dir = self.workdir / "backups"
        self.volume = f"{self.project}_pgdata"
        self.env_file = self.workdir / "stack.env"
        self.env_file.write_text(
            "\n".join(
                [
                    f"ATLAS_PUBLIC_URL=https://{PUBLIC_HOST}",
                    f"ATLAS_SECRETS_DIR={self.secrets_dir}",
                    f"ATLAS_BACKUP_DIR={self.backup_dir}",
                    f"ATLAS_PGDATA_VOLUME={self.volume}",
                    "ATLAS_IMAGE_TAG=local",
                    # The proxy is the only published port; tests reach it on loopback.
                    "ATLAS_BIND_ADDRESS=127.0.0.1",
                    f"ATLAS_HTTP_PORT={self.port}",
                    "TZ=UTC",
                    *(f"{k}={v}" for k, v in self.extra_env.items()),
                ]
            )
            + "\n"
        )
        # The API needs OIDC settings that atlas-init doesn't generate (Phase 5 /
        # TODO(spike-s)). Put them in app.env first; init only appends missing keys.
        self.secrets_dir.mkdir(mode=0o700)
        atlas_dir = self.secrets_dir / "atlas"
        atlas_dir.mkdir(mode=0o700)
        (atlas_dir / "app.env").write_text(
            "ATLAS_OIDC_ISSUER=https://id.test.invalid\n"
            f"ATLAS_OIDC_CLIENT_SECRET={secrets.token_urlsafe(24)}\n"
        )
        if self.create_volume:
            run(["docker", "volume", "create", self.volume])

    def compose(
        self, *args: str, check: bool = True, timeout: int = COMMAND_TIMEOUT
    ) -> subprocess.CompletedProcess[str]:
        cmd = ["docker", "compose", "-p", self.project, "--env-file", str(self.env_file)]
        cmd += ["-f", str(COMPOSE_FILE)]
        for overlay in self.overlays:
            cmd += ["-f", str(overlay)]
        return run([*cmd, *args], check=check, timeout=timeout)

    def diagnostics(self) -> str:
        """`compose ps` and recent logs of every service. Never raises."""
        parts = [f"===== diagnostics for compose project {self.project} ====="]

        def section(title: str, *args: str) -> None:
            try:
                result = self.compose(*args, check=False, timeout=60)
                body = (result.stdout + result.stderr).strip() or "(no output)"
            except Exception as exc:
                body = f"(could not collect: {exc})"
            parts.append(f"----- {title} -----\n{body[-6000:]}")

        section("docker compose ps --all", "ps", "--all")
        for service in DIAGNOSTIC_SERVICES:
            section(f"logs {service}", "logs", "--no-color", "--tail", "120", service)
        return "\n".join(parts)

    def fail(self, message: str) -> None:
        raise AssertionError(f"{message}\n{self.diagnostics()}")

    def up(self, *, check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            result = self.compose("up", "-d", "--no-build", check=False, timeout=UP_TIMEOUT)
        except HarnessTimeout as exc:
            self.fail(f"`docker compose up` did not return: {exc}")
            raise  # unreachable; fail() raises
        if check and result.returncode != 0:
            self.fail(
                f"`docker compose up` exited {result.returncode}\n"
                f"{(result.stdout + result.stderr)[-4000:]}"
            )
        return result

    def states(self) -> list[dict[str, object]]:
        result = self.compose("ps", "--all", "--format", "json", check=False)
        return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]

    def wait_healthy(self, timeout: float = HEALTHY_TIMEOUT) -> None:
        deadline = time.monotonic() + timeout
        last: list[dict[str, object]] = []
        while time.monotonic() < deadline:
            last = self.states()
            healthy = {s.get("Service") for s in last if s.get("Health") == "healthy"}
            if {"atlas-api", "atlas-proxy"} <= healthy:
                return
            exited = [s for s in last if s.get("State") == "exited" and s.get("ExitCode", 0) != 0]
            if exited:
                self.fail(f"a service exited with an error: {[s.get('Service') for s in exited]}")
            # `restart: unless-stopped` turns a failing start into a crash loop, which
            # never becomes healthy and never reads as exited: fail now, not at the limit.
            looping = [s for s in last if s.get("State") == "restarting"]
            if looping:
                self.fail(f"a service is crash-looping: {[s.get('Service') for s in looping]}")
            time.sleep(2)
        summary = {
            str(s.get("Service")): f"{s.get('State')}/{s.get('Health') or '-'}" for s in last
        }
        self.fail(f"stack not healthy within {timeout}s (state/health: {summary})")

    def wait_for_log(self, service: str, text: str, timeout: float = LOG_TIMEOUT) -> str:
        """Wait until `service` has logged `text` (e.g. a refusal to start)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            logs = self.logs(service)
            if text in logs:
                return logs
            time.sleep(2)
        self.fail(f"{service} did not log {text!r} within {timeout}s")
        return ""  # unreachable

    def port_unreachable(self) -> bool:
        try:
            self.request("/health")
        except (ConnectionError, http.client.HTTPException, OSError):
            return True
        return False

    def logs(self, *services: str) -> str:
        result = self.compose("logs", "--no-color", *services, check=False)
        return (result.stdout + result.stderr)[-8000:]

    def request(
        self,
        path: str,
        *,
        host: str = PUBLIC_HOST,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        """One request to the proxy's published port, with an explicit Host header."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=body, headers={"Host": host, **(headers or {})})
            response = conn.getresponse()
            return (
                response.status,
                {k.lower(): v for k, v in response.getheaders()},
                response.read(),
            )
        finally:
            conn.close()

    def get(self, path: str, *, host: str = PUBLIC_HOST) -> tuple[int, bytes]:
        status, _, body = self.request(path, host=host)
        return status, body

    def proxy_log(self) -> list[dict[str, object]]:
        """The proxy's JSON access-log lines."""
        out = self.compose("logs", "--no-color", "--no-log-prefix", "atlas-proxy", check=False)
        entries = []
        for line in out.stdout.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return entries

    def from_inside(self, path: str, *, headers: dict[str, str] | None = None) -> int:
        """A request to the proxy from another container on the stack's network.

        That container is neither loopback nor the gateway, so it is an untrusted peer.
        """
        script = (
            "import http.client,sys,json;"
            "c=http.client.HTTPConnection('atlas-proxy',8080,timeout=10);"
            f"c.request('GET',{path!r},headers={{'Host':{PUBLIC_HOST!r},**json.loads(sys.argv[1])}});"
            "print(c.getresponse().status)"
        )
        result = self.compose(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "--entrypoint",
            "python",
            "atlas-api",
            "-c",
            script,
            json.dumps(headers or {}),
        )
        return int(result.stdout.strip().splitlines()[-1])

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
        # Best effort and bounded: cleanup must never hang the job either.
        if self in ACTIVE:
            ACTIVE.remove(self)
        with contextlib.suppress(HarnessTimeout):
            self.compose(
                "--profile",
                "tools",
                "down",
                "--remove-orphans",
                "--timeout",
                "5",
                check=False,
                timeout=DOWN_TIMEOUT,
            )
        with contextlib.suppress(HarnessTimeout):
            run(["docker", "volume", "rm", "-f", self.volume], check=False)
        # Files under secrets/ and backups/ belong to container uids now; remove them
        # as root inside a container so pytest's tmp cleanup doesn't fail.
        with contextlib.suppress(HarnessTimeout):
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
