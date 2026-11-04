"""deploy/install.sh: external volumes (ADR-005 §4; bundled IdP, PLAN Phase 5a).

A stub `docker` records the calls; nothing touches a real daemon.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"


def run_install(tmp_path: Path, env_text: str, **environ: str) -> list[str]:
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    shutil.copy(DEPLOY / "install.sh", deploy / "install.sh")
    (deploy / ".env").write_text(env_text)
    log = tmp_path / "docker.log"
    stub = tmp_path / "bin"
    stub.mkdir()
    # `volume inspect` fails (no volume yet); everything else succeeds.
    (stub / "docker").write_text(
        f'#!/bin/sh\necho "$*" >> {log}\n[ "$1 $2" = "volume inspect" ] && exit 1\nexit 0\n'
    )
    (stub / "docker").chmod(0o755)
    result = subprocess.run(
        ["sh", str(deploy / "install.sh")],
        env={"PATH": f"{stub}:/usr/bin:/bin", **environ},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return log.read_text().splitlines()


def created(calls: list[str]) -> list[str]:
    return [c.split()[-1] for c in calls if c.startswith("volume create")]


def test_byo_creates_only_the_database_volume(tmp_path: Path) -> None:
    calls = run_install(tmp_path, "ATLAS_PUBLIC_URL=https://atlas.example.com\n")
    assert created(calls) == ["atlas_pgdata"]
    assert calls[-1] == "compose up -d"


@pytest.mark.parametrize("profiles", ["bundled-idp", "tools,bundled-idp", '"bundled-idp"'])
def test_bundled_idp_also_creates_the_idp_volume(tmp_path: Path, profiles: str) -> None:
    calls = run_install(tmp_path, f"COMPOSE_PROFILES={profiles}\nATLAS_IDP_VOLUME=my_idp\n")
    assert created(calls) == ["atlas_pgdata", "my_idp"]


def test_idp_volume_default_name(tmp_path: Path) -> None:
    calls = run_install(tmp_path, "COMPOSE_PROFILES=bundled-idp\n")
    assert created(calls) == ["atlas_pgdata", "atlas_idp"]


def test_environment_profiles_win(tmp_path: Path) -> None:
    # As in Compose: COMPOSE_PROFILES from the environment overrides deploy/.env.
    calls = run_install(tmp_path, "COMPOSE_PROFILES=bundled-idp\n", COMPOSE_PROFILES="")
    assert created(calls) == ["atlas_pgdata"]


def test_similar_profile_names_do_not_match(tmp_path: Path) -> None:
    calls = run_install(tmp_path, "COMPOSE_PROFILES=bundled-idp-old\n")
    assert created(calls) == ["atlas_pgdata"]
