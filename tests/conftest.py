import os
import secrets
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Must happen before anything imports core.*: the engine is created at import time.
# Overrides any DATABASE_URL from the environment (e.g. CI) so tests can never
# touch ./atlas.db or another real database.
_SESSION_DIR = Path(tempfile.mkdtemp(prefix="atlas-tests-"))
os.environ["DATABASE_URL"] = f"sqlite:///{_SESSION_DIR / 'import-time.db'}"
os.environ["ATLAS_DATA_DIR"] = str(_SESSION_DIR / "data")
os.environ["SECRET_KEY"] = secrets.token_urlsafe(32)
os.environ.pop("ATLAS_MAX_UPLOAD_BYTES", None)
os.environ.pop("STORAGE_BACKEND", None)

TEST_PASSWORD = "correct-horse-battery"


@pytest.fixture(scope="session", autouse=True)
def _never_touch_repo_db():
    repo_db = PROJECT_ROOT / "atlas.db"
    before = repo_db.stat().st_mtime if repo_db.exists() else None
    yield
    after = repo_db.stat().st_mtime if repo_db.exists() else None
    assert after == before, "tests created or modified ./atlas.db"


@pytest.fixture
def test_paths(tmp_path, monkeypatch):
    """Per-test database + data dir, wired into every place the app looks for them."""
    db_path = tmp_path / "test.db"
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ATLAS_DATA_DIR", str(data_dir))

    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    from core.db.models import Base
    Base.metadata.create_all(bind=engine)

    import core.db.init as init_mod
    import core.db.session as session_mod
    import core.main as main_mod
    monkeypatch.setattr(session_mod, "SessionLocal", TestSession)
    monkeypatch.setattr(main_mod, "SessionLocal", TestSession)
    monkeypatch.setattr(init_mod, "engine", engine)

    yield {"db_path": db_path, "data_dir": data_dir, "session_factory": TestSession}
    engine.dispose()


@pytest.fixture
def db_session(test_paths):
    db = test_paths["session_factory"]()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def app_client(test_paths):
    from core.db.session import get_db
    from core.main import app

    TestSession = test_paths["session_factory"]

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


def register_and_login(client, username: str, password: str = TEST_PASSWORD) -> dict:
    r = client.post("/auth/register", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    r = client.post("/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
def auth_headers(app_client):
    return register_and_login(app_client, "alice")


@pytest.fixture
def two_users(app_client):
    return {
        "alice": register_and_login(app_client, "alice"),
        "bob": register_and_login(app_client, "bob"),
    }


@pytest.fixture
def upload(app_client):
    def _upload(headers, name="file.txt", content=b"data", mime="text/plain", parent_id=None):
        url = f"/objects/upload?obj_type=file&name={name}"
        if parent_id is not None:
            url += f"&parent_id={parent_id}"
        return app_client.post(url, files={"file": (name, content, mime)}, headers=headers)
    return _upload


@pytest.fixture
def create_folder(app_client):
    def _create(headers, name="folder", parent_id=None):
        return app_client.post("/folders", json={"name": name, "parent_id": parent_id}, headers=headers)
    return _create
