import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


# Ensure project root is importable: allows `import core...`
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    """
    Provides a TestClient with isolated DATABASE_URL and ATLAS_DATA_DIR per test.
    This fixture imports the app AFTER env vars are set so your engine picks them up.
    """
    db_path = tmp_path / "test.db"
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("ATLAS_DATA_DIR", str(data_dir))

    from core.main import app  # import after env vars

    with TestClient(app) as client:
        yield client


@pytest.fixture
def test_paths(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("ATLAS_DATA_DIR", str(data_dir))

    return {"db_path": db_path, "data_dir": data_dir}
