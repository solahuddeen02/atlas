"""Helpers for calling PostgreSQL client tools without exposing passwords."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from pydantic import SecretStr
from sqlalchemy.engine import URL, make_url

APP_ROLE = "atlas_app"
MIGRATOR_ROLE = "atlas_migrator"


class PgToolError(RuntimeError):
    pass


def parse_url(url: SecretStr) -> URL:
    return make_url(url.get_secret_value())


def run_pg_tool(tool: str, url: SecretStr, args: list[str]) -> None:
    """Run pg_dump/pg_restore against `url`. The password goes in PGPASSWORD, not argv."""
    exe = shutil.which(tool)
    if exe is None:
        raise PgToolError(f"{Path(tool).name} not found on PATH")
    parsed = parse_url(url)
    conn_args = ["--host", parsed.host or "localhost", "--port", str(parsed.port or 5432)]
    conn_args += ["--username", parsed.username or "", "--dbname", parsed.database or ""]
    # A minimal, explicit environment: nothing else from the process leaks into the tool.
    env = {"PGPASSWORD": parsed.password or "", "PGTZ": "UTC", "PGCONNECT_TIMEOUT": "10"}
    result = subprocess.run(
        [exe, *conn_args, *args], env=env, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        # pg tools never print the password; stderr is safe to surface.
        detail = result.stderr.strip().splitlines()[-1:] or ["no output"]
        raise PgToolError(f"{Path(tool).name} failed (exit {result.returncode}): {detail[0]}")
