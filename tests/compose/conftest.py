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


# The watchdog writes here, because pytest captures stderr during a test. The
# compose CI job prints this file in its diagnostics step.
WATCHDOG_LOG = REPO_ROOT / "compose-watchdog.log"


def _watchdog_fire(nodeid: str) -> None:
    with WATCHDOG_LOG.open("a") as out:
        out.write(f"!!!!! {nodeid} exceeded {TEST_LIMIT_SECONDS}s; aborting the run !!!!!\n")
        out.flush()
        for stack in list(ACTIVE):
            out.write(stack.diagnostics() + "\n")
            out.flush()
        out.write("----- Python tracebacks -----\n")
        out.flush()
        faulthandler.dump_traceback(file=out, all_threads=True)
    os._exit(3)


@pytest.fixture(autouse=True)
def compose_watchdog(request: pytest.FixtureRequest) -> Iterator[None]:
    timer = threading.Timer(TEST_LIMIT_SECONDS, _watchdog_fire, args=(request.node.nodeid,))
    timer.daemon = True
    timer.start()
    # Backstop in C: exits even if the Python watchdog (or its diagnostics) is stuck.
    backstop = WATCHDOG_LOG.open("a")
    faulthandler.dump_traceback_later(TEST_LIMIT_SECONDS + 180, exit=True, file=backstop)
    try:
        yield
    finally:
        faulthandler.cancel_dump_traceback_later()
        backstop.close()
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


# --- Shared stacks (read-only tests only) ---------------------------------------------------
# One default stack per mode for the whole session, for tests that only read: HTTP requests,
# `compose run --rm` one-offs, SELECTs, inspect and log reads. A test that changes a stack's
# state, restarts or stops a service, sets another env value, or breaks the stack on purpose
# uses `make_stack` instead. Before each test the shared stack must still be healthy; if not,
# the test that ran on it before is named, so one test breaking it is not blamed on the next.

LAST_ON: dict[str, str] = {}  # project -> the last test that used that shared stack


def _shared(
    tmp_path_factory: pytest.TempPathFactory, name: str, **kwargs: object
) -> Iterator[Stack]:
    stack = Stack(workdir=tmp_path_factory.mktemp(name), **kwargs)  # type: ignore[arg-type]
    try:
        stack.up()
        stack.wait_healthy()
        yield stack
    finally:
        stack.destroy()  # by project name: compose down (never -v) and its own volumes


def _guard(stack: Stack, request: pytest.FixtureRequest) -> Iterator[Stack]:
    needed = {"atlas-api", "atlas-proxy"} | ({"idp"} if stack.idp else set())
    healthy = {str(s.get("Service")) for s in stack.states() if s.get("Health") == "healthy"}
    if not needed <= healthy:
        stack.fail(
            f"the shared stack {stack.project} is not healthy before {request.node.nodeid} "
            f"(healthy: {sorted(healthy)}, needed: {sorted(needed)}). It was healthy when it "
            f"came up, so the test that used it before this one broke it: "
            f"{LAST_ON.get(stack.project, 'none')}"
        )
    LAST_ON[stack.project] = request.node.nodeid
    yield stack
    if request.node.stash.get(FAILED, False):
        print(stack.diagnostics(), file=sys.stderr)


@pytest.fixture(scope="session")
def _shared_byo_stack(
    tmp_path_factory: pytest.TempPathFactory, atlas_image: str
) -> Iterator[Stack]:
    yield from _shared(tmp_path_factory, "shared-byo")


@pytest.fixture(scope="session")
def _shared_bundled_stack(
    tmp_path_factory: pytest.TempPathFactory, atlas_image: str
) -> Iterator[Stack]:
    yield from _shared(tmp_path_factory, "shared-bundled", idp=True)


@pytest.fixture
def shared_byo(_shared_byo_stack: Stack, request: pytest.FixtureRequest) -> Iterator[Stack]:
    """The session's default BYO-OIDC stack, healthy; for read-only tests."""
    yield from _guard(_shared_byo_stack, request)


@pytest.fixture
def shared_bundled(_shared_bundled_stack: Stack, request: pytest.FixtureRequest) -> Iterator[Stack]:
    """The session's default bundled-IdP stack, healthy; for read-only tests."""
    yield from _guard(_shared_bundled_stack, request)
