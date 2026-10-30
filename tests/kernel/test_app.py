from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from atlas.kernel.config import Settings
from atlas.kernel.web import create_app


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
