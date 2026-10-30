"""The API process refuses to start on bad config, before binding a socket.

The container-level version of this test arrives with the image in Phase 4.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import VALID


def start(tmp_path: Path, **overrides: str) -> subprocess.CompletedProcess[str]:
    app_env = tmp_path / "app.env"
    values = {k: v for k, v in VALID.items() if k != "ATLAS_ENV"}
    app_env.write_text("".join(f"{k}={v}\n" for k, v in values.items()))
    # A minimal, explicit environment: nothing from the developer's shell leaks in.
    env = {"ATLAS_ENV_FILES": str(app_env), **overrides}
    return subprocess.run(
        [sys.executable, "-m", "atlas.kernel.web", "--host", "127.0.0.1", "--port", "1"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


@pytest.mark.parametrize("placeholder", ["dev-secret-change-in-production", "changeme"])
def test_placeholder_key_exits_before_serving(tmp_path: Path, placeholder: str) -> None:
    result = start(tmp_path, SECRET_KEY=placeholder)
    assert result.returncode == 2
    assert "SECRET_KEY" in result.stderr
    assert placeholder not in result.stderr + result.stdout
    assert "Uvicorn running" not in result.stderr + result.stdout
    assert "Started server process" not in result.stderr + result.stdout


def test_missing_key_exits(tmp_path: Path) -> None:
    app_env = tmp_path / "empty.env"
    app_env.write_text("")
    result = subprocess.run(
        [sys.executable, "-m", "atlas.kernel.web"],
        env={"ATLAS_ENV_FILES": str(app_env)},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 2
    assert "SECRET_KEY: is required" in result.stderr


def test_production_placeholder_url_exits(tmp_path: Path) -> None:
    result = start(tmp_path, ATLAS_PUBLIC_URL="https://atlas.example.com")
    assert result.returncode == 2
    assert "ATLAS_PUBLIC_URL: still uses a placeholder domain" in result.stderr
