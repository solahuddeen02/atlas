import faulthandler
import os
import sys
import threading
from collections.abc import Callable, Generator, Iterator
from pathlib import Path

import pytest

from tests.compose.stack import ACTIVE, DEPLOY, IMAGE, REPO_ROOT, Stack, run
from tests.conftest import RUN_COMPOSE_TESTS

# Hard ceiling for one compose test, including fixture setup and teardown. Past it
# the watchdog dumps the stack's state and logs plus every thread's traceback, then
# exits the run: a stuck test must fail loudly, never time the CI job out silently.
TEST_LIMIT_SECONDS = 480

# Set on an item when any phase of it failed; read by make_stack's teardown.
FAILED = pytest.StashKey[bool]()


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "tests/compose" in str(item.path):
            item.add_marker(pytest.mark.compose)
            if not RUN_COMPOSE_TESTS:
                item.add_marker(pytest.mark.skip(reason="needs Docker; set RUN_COMPOSE_TESTS=1"))


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, None]:
    outcome = yield
    report = outcome.get_result()
    # Remembered so make_stack's teardown knows whether the test failed.
    item.stash.setdefault(FAILED, False)
    if report.failed:
        item.stash[FAILED] = True


def _watchdog_fire(nodeid: str) -> None:
    out = sys.__stderr__ or sys.stderr
    out.write(f"\n\n!!!!! {nodeid} exceeded {TEST_LIMIT_SECONDS}s; aborting the run !!!!!\n")
    for stack in list(ACTIVE):
        out.write(stack.diagnostics() + "\n")
    out.write("----- Python tracebacks -----\n")
    out.flush()
    faulthandler.dump_traceback(file=out, all_threads=True)
    out.flush()
    os._exit(3)


@pytest.fixture(autouse=True)
def compose_watchdog(request: pytest.FixtureRequest) -> Iterator[None]:
    timer = threading.Timer(TEST_LIMIT_SECONDS, _watchdog_fire, args=(request.node.nodeid,))
    timer.daemon = True
    timer.start()
    try:
        yield
    finally:
        timer.cancel()


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
def make_stack(
    request: pytest.FixtureRequest, tmp_path: Path, atlas_image: str
) -> Iterator[Callable[..., Stack]]:
    stacks: list[Stack] = []

    def make(name: str = "a", **kwargs: object) -> Stack:
        workdir = tmp_path / name
        workdir.mkdir()
        stack = Stack(workdir=workdir, **kwargs)  # type: ignore[arg-type]
        stacks.append(stack)
        return stack

    yield make
    failed = request.node.stash.get(FAILED, False)
    for stack in stacks:
        if failed:
            # Shown in the failure report ("Captured stderr teardown").
            print(stack.diagnostics(), file=sys.stderr)
        stack.destroy()
