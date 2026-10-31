from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from tests.compose.stack import DEPLOY, IMAGE, REPO_ROOT, Stack, run
from tests.conftest import RUN_COMPOSE_TESTS


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "tests/compose" in str(item.path):
            item.add_marker(pytest.mark.compose)
            if not RUN_COMPOSE_TESTS:
                item.add_marker(pytest.mark.skip(reason="needs Docker; set RUN_COMPOSE_TESTS=1"))


@pytest.fixture(scope="session")
def atlas_image() -> str:
    # The compose CI job builds it first; build here too so a local run works.
    if run(["docker", "image", "inspect", IMAGE], check=False).returncode != 0:
        run(["docker", "build", "-t", IMAGE, str(REPO_ROOT)], timeout=1800)
    return IMAGE


@pytest.fixture(scope="session")
def failing_migration_image(atlas_image: str) -> str:
    run(
        [
            "docker",
            "build",
            "-t",
            "atlas:failing-migration",
            "--build-arg",
            f"BASE={atlas_image}",
            "-f",
            str(DEPLOY / "test/Dockerfile.failing-migration"),
            str(DEPLOY / "test"),
        ],
        timeout=600,
    )
    return "atlas:failing-migration"


@pytest.fixture
def make_stack(tmp_path: Path, atlas_image: str) -> Iterator[Callable[..., Stack]]:
    stacks: list[Stack] = []

    def make(name: str = "a", **kwargs: object) -> Stack:
        workdir = tmp_path / name
        workdir.mkdir()
        stack = Stack(workdir=workdir, **kwargs)  # type: ignore[arg-type]
        stacks.append(stack)
        return stack

    yield make
    for stack in stacks:
        stack.destroy()
