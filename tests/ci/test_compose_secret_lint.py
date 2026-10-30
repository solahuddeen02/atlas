import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "ci" / "lint_compose_secrets.py"


def run_lint(tmp_path: Path, compose: str) -> subprocess.CompletedProcess[str]:
    path = tmp_path / "docker-compose.yml"
    path.write_text(textwrap.dedent(compose))
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(path)], capture_output=True, text=True, check=False
    )


@pytest.mark.parametrize(
    "environment",
    [
        "- SECRET_KEY=abc123",
        "- MINIO_ROOT_PASSWORD=minioadmin",
        "- API_TOKEN=${API_TOKEN:-dev-token}",
        "SECRET_KEY: abc123",
        "db_password: hunter2",
    ],
)
def test_literal_secret_fails(tmp_path: Path, environment: str) -> None:
    result = run_lint(
        tmp_path,
        f"""
        services:
          app:
            environment:
              {environment}
        """,
    )
    assert result.returncode == 1
    assert "literal value" in result.stderr


def test_literal_value_is_not_printed(tmp_path: Path) -> None:
    result = run_lint(
        tmp_path,
        """
        services:
          app:
            environment:
              SECRET_KEY: do-not-print-me
        """,
    )
    assert result.returncode == 1
    assert "do-not-print-me" not in result.stderr + result.stdout


@pytest.mark.parametrize(
    "environment",
    [
        "- SECRET_KEY",
        "- SECRET_KEY=${SECRET_KEY}",
        "- MINIO_ROOT_PASSWORD=${MINIO_ROOT_PASSWORD:?set it in .env}",
        "- POSTGRES_PASSWORD_FILE=/run/secrets/postgres/password",
        "- STORAGE_BACKEND=local",
        "SECRET_KEY:",
        "SECRET_KEY: ${SECRET_KEY?missing}",
        "TZ: UTC",
    ],
)
def test_allowed_forms_pass(tmp_path: Path, environment: str) -> None:
    result = run_lint(
        tmp_path,
        f"""
        services:
          app:
            environment:
              {environment}
        """,
    )
    assert result.returncode == 0, result.stderr


def test_repository_compose_files_are_clean() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "0 problem(s)" in result.stdout
