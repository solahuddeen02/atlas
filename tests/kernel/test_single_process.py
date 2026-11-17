"""atlas-api runs as ONE process (PLAN Phase 5b-3a, owner answer 6).

The IdentityAdmin adapter serialises a person's role changes with an in-process lock. Two
processes would race: probe P5 ran two role changes for one person in parallel and left
the person in two role groups in 20 of 20 rounds. If more than one process is ever
wanted, a cross-process lock comes first: a Postgres advisory lock around each role
change (`pg_advisory_xact_lock` keyed on the person), then this test changes with it.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from atlas.kernel.web import __main__ as entry
from tests.conftest import VALID

NEEDS_LOCK = (
    "atlas-api must run as one process until role changes hold a cross-process lock "
    "(a Postgres advisory lock around each change, see atlas/kernel/web/__main__.py)"
)


def run_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **env: str) -> dict[str, Any]:
    import uvicorn

    app_env = tmp_path / "app.env"
    values = {k: v for k, v in VALID.items() if k != "ATLAS_ENV"}
    app_env.write_text("".join(f"{k}={v}\n" for k, v in values.items()))
    monkeypatch.setenv("ATLAS_ENV_FILES", str(app_env))
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    seen: dict[str, Any] = {}

    def fake_run(app: object, **kwargs: Any) -> None:
        seen["app"] = app
        seen.update(kwargs)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    assert entry.main(["--host", "127.0.0.1", "--port", "1"]) == 0
    return seen


def test_uvicorn_gets_an_app_object_and_one_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = run_main(tmp_path, monkeypatch)
    # An import string is how uvicorn starts several workers; an object cannot be forked.
    assert not isinstance(seen["app"], str), NEEDS_LOCK
    assert seen.get("workers", 1) == 1, NEEDS_LOCK
    assert not seen.get("reload"), NEEDS_LOCK


def test_the_workers_environment_variable_cannot_raise_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # uvicorn reads WEB_CONCURRENCY when `workers` is not given; the call gives it.
    seen = run_main(tmp_path, monkeypatch, WEB_CONCURRENCY="4")
    assert seen["workers"] == 1, NEEDS_LOCK


def test_the_command_line_has_no_workers_option() -> None:
    with pytest.raises(SystemExit) as raised:
        entry.main(["--workers", "2"])
    assert raised.value.code == 2, NEEDS_LOCK


def test_no_service_in_the_compose_file_asks_for_more_than_one_process() -> None:
    text = Path("deploy/docker-compose.yml").read_text()
    for needle in ("--workers", "WEB_CONCURRENCY", "UVICORN_WORKERS", "gunicorn"):
        assert needle not in text, f"{needle}: {NEEDS_LOCK}"
    assert sys.version_info >= (3, 11)
