"""The completion record atlas-api checks before it starts (PLAN Phase 5b-2b)."""

import json
from pathlib import Path

import pytest

from atlas.kernel.identity.record import (
    BOOTSTRAP_VERSION,
    LOGS_HINT,
    read_record,
    record_problem,
    record_text,
)

DIGEST = "0" * 64


def write(path: Path, data: object) -> Path:
    path.write_text(data if isinstance(data, str) else json.dumps(data))
    return path


def test_a_complete_record_of_this_version_is_accepted(tmp_path: Path) -> None:
    path = write(tmp_path / "bootstrap.json", record_text(digest=DIGEST, complete=True))
    assert record_problem(path) is None
    assert read_record(path) == {"version": BOOTSTRAP_VERSION, "digest": DIGEST, "complete": True}


def test_an_incomplete_record_is_refused(tmp_path: Path) -> None:
    path = write(tmp_path / "bootstrap.json", record_text(digest=DIGEST, complete=False))
    problem = record_problem(path)
    assert problem is not None and "has not completed" in problem


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (None, "has not run yet"),  # no file at all
        ("", "unreadable"),
        ("not json", "unreadable"),
        ("[]", "unreadable"),
        ("null", "unreadable"),
        (b"\xff\xfe", "unreadable"),
        ({"complete": True}, "version ?"),  # no version
        ({"version": BOOTSTRAP_VERSION - 1, "complete": True}, f"version {BOOTSTRAP_VERSION - 1}"),
        ({"version": BOOTSTRAP_VERSION + 1, "complete": True}, f"version {BOOTSTRAP_VERSION + 1}"),
        ({"version": True, "complete": True}, "version ?"),
        ({"version": "1", "complete": True}, "version ?"),
        ({"version": BOOTSTRAP_VERSION}, "has not completed"),
        ({"version": BOOTSTRAP_VERSION, "complete": "true"}, "has not completed"),
        ({"version": BOOTSTRAP_VERSION, "complete": 1}, "has not completed"),
        ({"version": BOOTSTRAP_VERSION, "complete": None}, "has not completed"),
        ({"version": BOOTSTRAP_VERSION, "in_progress": False}, "has not completed"),
    ],
)
def test_anything_but_a_complete_record_of_this_version_is_refused(
    tmp_path: Path, content: object, reason: str
) -> None:
    path = tmp_path / "bootstrap.json"
    if isinstance(content, bytes):
        path.write_bytes(content)
    elif content is not None:
        write(path, content)
    problem = record_problem(path)
    assert problem is not None and reason in problem
    # Every refusal says where to look and what to do.
    assert "docker compose logs atlas-idp-bootstrap" in problem
    assert "docker compose up -d" in problem
    assert problem.endswith(LOGS_HINT)


def test_a_directory_is_not_a_record(tmp_path: Path) -> None:
    problem = record_problem(tmp_path)
    assert problem is not None and "has not run yet" in problem


def test_a_refusal_never_repeats_what_the_file_holds(tmp_path: Path) -> None:
    path = write(tmp_path / "bootstrap.json", {"version": "leaky-value-123", "complete": True})
    problem = record_problem(path)
    assert problem is not None and "leaky-value-123" not in problem
