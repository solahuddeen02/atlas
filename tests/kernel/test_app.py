from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from atlas.kernel.config import Settings
from atlas.kernel.web import create_app
from tests.conftest import PgTestDb


def client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings))


def test_health_is_exactly_ok(make_settings: Callable[..., Settings]) -> None:
    response = client(make_settings(ATLAS_ENV="production")).get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.content == b'{"status":"ok"}'


@pytest.mark.parametrize("path", ["/docs", "/openapi.json", "/redoc"])
def test_docs_off_in_production(make_settings: Callable[..., Settings], path: str) -> None:
    assert client(make_settings(ATLAS_ENV="production")).get(path).status_code == 404


@pytest.mark.parametrize("path", ["/docs", "/openapi.json"])
def test_docs_on_in_development(make_settings: Callable[..., Settings], path: str) -> None:
    assert client(make_settings(ATLAS_ENV="development")).get(path).status_code == 200


def test_cors_only_outside_production(make_settings: Callable[..., Settings]) -> None:
    origin = "http://localhost:5173"
    headers = {"Origin": origin}

    dev = client(make_settings(ATLAS_ENV="development", ATLAS_DEV_CORS_ORIGINS=origin))
    assert dev.get("/health", headers=headers).headers.get("access-control-allow-origin") == origin

    prod = client(make_settings(ATLAS_ENV="production", ATLAS_DEV_CORS_ORIGINS=origin))
    assert "access-control-allow-origin" not in prod.get("/health", headers=headers).headers


def test_ready_reports_database(
    migrated_db: PgTestDb, make_settings: Callable[..., Settings]
) -> None:
    from atlas.kernel.db.engine import make_engine

    engine = make_engine(migrated_db.settings.database_url)
    try:
        app = create_app(make_settings(ATLAS_ENV="production"), engine=engine)
        response = TestClient(app).get("/health/ready")
        assert response.status_code == 200
        assert response.json() == {"status": "ready"}
    finally:
        engine.dispose()


def test_ready_hides_why_it_is_not_ready(make_settings: Callable[..., Settings]) -> None:
    from pydantic import SecretStr

    from atlas.kernel.db.engine import make_engine

    # Nothing listens on port 1: the database is unreachable.
    url = SecretStr("postgresql+psycopg://atlas_app:Qm4tW9zLr2Xv@127.0.0.1:1/atlas")
    engine = make_engine(url)
    try:
        app = create_app(make_settings(ATLAS_ENV="production"), engine=engine)
        response = TestClient(app).get("/health/ready")
        assert response.status_code == 503
        assert response.content == b'{"status":"not_ready"}'
    finally:
        engine.dispose()


def test_health_does_not_touch_the_database(make_settings: Callable[..., Settings]) -> None:
    # No engine and no reachable database: /health still answers.
    app = create_app(make_settings(ATLAS_ENV="production"))
    assert TestClient(app).get("/health").json() == {"status": "ok"}


def test_ready_not_in_schema(make_settings: Callable[..., Settings]) -> None:
    schema = client(make_settings(ATLAS_ENV="development")).get("/openapi.json").json()
    assert "/health/ready" not in schema["paths"]
    assert "/health" not in schema["paths"]
