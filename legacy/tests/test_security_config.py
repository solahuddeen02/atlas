import os
import subprocess
import sys

import pytest
from jose import jwt

from core.security import InsecureConfigError, validate_secret_key
from conftest import PROJECT_ROOT

OLD_DEFAULT_KEY = "dev-secret-change-in-production"


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "too-short-key",
        "a" * 64,  # long but repetitive
        OLD_DEFAULT_KEY,
        "change-this-in-production",
        "changeme",
        "password",
        "please-CHANGE-me-before-PRODUCTION-0123456789abcdefghij",
    ],
)
def test_secret_key_fail_closed(value):
    with pytest.raises(InsecureConfigError) as exc:
        validate_secret_key(value)
    assert "SECRET_KEY" in str(exc.value)
    if value:
        assert value not in str(exc.value)


def test_strong_secret_key_accepted():
    import secrets
    key = secrets.token_urlsafe(32)
    assert validate_secret_key(key) == key


@pytest.mark.parametrize("value", [None, OLD_DEFAULT_KEY, "change-this-in-production"])
def test_server_refuses_to_start_with_bad_secret_key(tmp_path, value):
    env = {k: v for k, v in os.environ.items() if k != "SECRET_KEY"}
    if value is not None:
        env["SECRET_KEY"] = value
    env["DATABASE_URL"] = f"sqlite:///{tmp_path / 'never.db'}"
    env["ATLAS_DATA_DIR"] = str(tmp_path / "data")
    env["PYTHONPATH"] = str(PROJECT_ROOT)

    proc = subprocess.run(
        [sys.executable, "-m", "uvicorn", "core.main:app", "--host", "127.0.0.1", "--port", "0"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0
    assert "SECRET_KEY" in output
    if value:
        assert value not in output
    assert not (tmp_path / "never.db").exists()


def test_forged_default_token_rejected(app_client, auth_headers):
    # A token signed with the old public default key must not be accepted.
    forged = jwt.encode({"sub": "1", "username": "alice", "exp": 9999999999}, OLD_DEFAULT_KEY, algorithm="HS256")
    r = app_client.get("/objects", headers={"Authorization": f"Bearer {forged}"})
    assert r.status_code == 401
    # The real token for the same user still works.
    assert app_client.get("/objects", headers=auth_headers).status_code == 200


def test_minio_requires_credentials(monkeypatch):
    from core.storage.minio import MinIOStorage
    monkeypatch.delenv("MINIO_ACCESS_KEY", raising=False)
    monkeypatch.delenv("MINIO_SECRET_KEY", raising=False)
    with pytest.raises(RuntimeError, match="MINIO_ACCESS_KEY"):
        MinIOStorage()
