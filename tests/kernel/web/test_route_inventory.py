"""Route inventory: every route that can change something needs a session (ADR-002, ADR-003 §4).

A route with a non-safe method (anything but GET, HEAD and OPTIONS) must depend on
`current_session`, directly or through another dependency such as `require_step_up`. That
is what puts the CSRF token and the `Origin` rule in front of it. A route that is allowed
to go without must be listed in `UNPROTECTED_ALLOWED` below, with the reason in one line.
"""

from collections.abc import Callable
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from starlette.applications import Starlette
from starlette.routing import Mount, Route, WebSocketRoute

from atlas.kernel.config import Settings
from atlas.kernel.identity.sessions import Session
from atlas.kernel.web import create_app
from atlas.kernel.web.auth import SAFE_METHODS, current_session, require_step_up

# "METHOD /path" -> why this route may take that method without a session.
# Empty today: nothing in the API changes anything without one. A login's callback and
# similar are GET (and carry their own state check), so they are not here.
UNPROTECTED_ALLOWED: dict[str, str] = {}


def depends_on_current_session(dependant: Dependant) -> bool:
    return any(
        dep.call is current_session or depends_on_current_session(dep)
        for dep in dependant.dependencies
    )


def unprotected(app: FastAPI, allowed: dict[str, str] | None = None) -> list[str]:
    """`"METHOD /path"` for each route that takes an unsafe method without a session.

    A mounted app or a WebSocket cannot be inspected here, so each is reported as
    `"MOUNT /path"` or `"WEBSOCKET /path"` unless it is in `allowed`.
    """
    allowed = UNPROTECTED_ALLOWED if allowed is None else allowed
    found: list[str] = []
    for route in app.routes:
        if isinstance(route, APIRoute):
            for method in sorted(set(route.methods or ()) - SAFE_METHODS):
                if not depends_on_current_session(route.dependant):
                    found.append(f"{method} {route.path}")
        elif isinstance(route, Mount):
            found.append(f"MOUNT {route.path}")
        elif isinstance(route, WebSocketRoute):
            found.append(f"WEBSOCKET {route.path}")
        elif isinstance(route, Route):  # a plain Starlette route: its dependencies are opaque
            for method in sorted(set(route.methods or ()) - SAFE_METHODS):
                found.append(f"{method} {route.path}")
    return [entry for entry in found if entry not in allowed]


@pytest.mark.parametrize("env", ["production", "development"])
def test_every_route_that_can_change_something_needs_a_session(
    make_settings: Callable[..., Settings], env: str
) -> None:
    app = create_app(make_settings(ATLAS_ENV=env))
    assert unprotected(app) == []
    assert app.routes  # the walk saw something


def test_the_allowlist_gives_a_reason_for_each_entry_and_has_no_stale_one(
    make_settings: Callable[..., Settings],
) -> None:
    app = create_app(make_settings())
    paths = {
        f"{m} {r.path}" for r in app.routes if isinstance(r, APIRoute) for m in r.methods or ()
    }
    for entry, reason in UNPROTECTED_ALLOWED.items():
        assert reason.strip() and "\n" not in reason, entry
        assert entry in paths, f"{entry} is allowed but there is no such route"


# --- the checker itself: it fails when it should ----------------------------------------------


@pytest.fixture
def app(make_settings: Callable[..., Settings]) -> FastAPI:
    return create_app(make_settings())


def test_a_post_route_without_the_dependency_is_reported(app: FastAPI) -> None:
    @app.post("/t/unprotected")
    def unprotected_post() -> dict[str, bool]:
        return {"ok": True}

    assert unprotected(app) == ["POST /t/unprotected"]


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_every_unsafe_method_is_checked(app: FastAPI, method: str) -> None:
    app.add_api_route("/t/x", lambda: {"ok": True}, methods=[method])
    assert unprotected(app) == [f"{method} /t/x"]


def test_only_the_unsafe_method_of_a_mixed_route_is_reported(app: FastAPI) -> None:
    app.add_api_route("/t/mixed", lambda: {"ok": True}, methods=["GET", "POST"])
    assert unprotected(app) == ["POST /t/mixed"]


def test_a_safe_route_needs_no_session(app: FastAPI) -> None:
    app.add_api_route("/t/read", lambda: {"ok": True}, methods=["GET", "HEAD", "OPTIONS"])
    assert unprotected(app) == []


def test_the_session_dependency_protects_a_route(app: FastAPI) -> None:
    @app.post("/t/protected")
    def protected(session: Annotated[Session, Depends(current_session)]) -> dict[str, bool]:
        return {"ok": True}

    assert unprotected(app) == []


def test_step_up_protects_a_route_through_the_session_dependency(app: FastAPI) -> None:
    @app.delete("/t/admin")
    def admin(session: Annotated[Session, Depends(require_step_up())]) -> dict[str, bool]:
        return {"ok": True}

    assert unprotected(app) == []


def test_a_dependency_in_the_decorator_protects_a_route(app: FastAPI) -> None:
    @app.put("/t/decorated", dependencies=[Depends(current_session)])
    def decorated() -> dict[str, bool]:
        return {"ok": True}

    assert unprotected(app) == []


def test_another_dependency_is_not_enough(app: FastAPI) -> None:
    def something_else() -> str:
        return "no session here"

    @app.post("/t/other")
    def other(value: Annotated[str, Depends(something_else)]) -> dict[str, bool]:
        return {"ok": True}

    assert unprotected(app) == ["POST /t/other"]


def test_a_mounted_app_and_a_websocket_cannot_be_checked_so_they_are_reported(
    app: FastAPI,
) -> None:
    app.mount("/files", Starlette())
    app.routes.append(WebSocketRoute("/ws", lambda websocket: None))
    assert unprotected(app) == ["MOUNT /files", "WEBSOCKET /ws"]


def test_a_plain_starlette_route_is_reported(app: FastAPI) -> None:
    app.routes.append(Route("/t/plain", lambda request: None, methods=["POST"]))
    assert unprotected(app) == ["POST /t/plain"]


def test_a_listed_route_is_let_through_with_its_reason(app: FastAPI) -> None:
    app.add_api_route("/t/hook", lambda: {"ok": True}, methods=["POST"])
    assert unprotected(app) == ["POST /t/hook"]
    assert unprotected(app, {"POST /t/hook": "a signed webhook; checks its own signature"}) == []
