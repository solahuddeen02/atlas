import sys
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _make_test_engine(db_path):
    return create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("ATLAS_DATA_DIR", str(data_dir))

    test_engine = _make_test_engine(db_path)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)

    from core.db.models import Base
    Base.metadata.create_all(bind=test_engine)

    from core.main import app
    from core.db.session import get_db

    def override_get_db():
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app) as client:
        yield client

    app.dependency_overrides.clear()


@pytest.fixture
def auth_client(app_client):
    """app_client with a registered + logged-in user (Bearer token attached).

    Protected endpoints resolve get_current_user via the Authorization header,
    so tests hitting /folders, /objects, /drive, etc. should use this fixture.
    """
    app_client.post(
        "/auth/register",
        json={"username": "tester", "password": "secret123"},
    )
    login = app_client.post(
        "/auth/login",
        json={"username": "tester", "password": "secret123"},
    )
    token = login.json()["access_token"]
    app_client.headers["Authorization"] = f"Bearer {token}"
    return app_client


@pytest.fixture
def test_paths(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("ATLAS_DATA_DIR", str(data_dir))

    test_engine = _make_test_engine(db_path)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)

    from core.db.models import Base
    Base.metadata.create_all(bind=test_engine)

    import core.db.session as session_mod
    session_mod.__dict__["SessionLocal"] = TestSession

    return {"db_path": db_path, "data_dir": data_dir}
