import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def ruff(*args: str, input: str) -> subprocess.CompletedProcess[str]:
    exe = shutil.which("ruff")
    cmd = [exe] if exe else [sys.executable, "-m", "ruff"]
    return subprocess.run(
        [*cmd, "check", "--no-cache", *args],
        input=input,
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    )


@pytest.mark.parametrize(
    "source",
    [
        "import os\nx = os.getenv('X')\n",
        "import os\nx = os.environ['X']\n",
        "import os\nx = os.environ.get('X')\n",
        "from os import getenv\nx = getenv('X')\n",
        "from os import environ\nx = environ['X']\n",
    ],
)
def test_env_access_outside_config_is_rejected(source: str) -> None:
    result = ruff(
        "--select", "TID251", "--stdin-filename", "atlas/kernel/somewhere.py", "-", input=source
    )
    assert result.returncode == 1, result.stdout
    assert "TID251" in result.stdout


def test_env_access_allowed_in_config_module() -> None:
    result = ruff(
        "--select",
        "TID251",
        "--stdin-filename",
        "atlas/kernel/config.py",
        "-",
        input="import os\nx = os.getenv('X')\n",
    )
    assert result.returncode == 0, result.stdout
