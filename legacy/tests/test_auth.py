def test_register_success(app_client):
    r = app_client.post("/auth/register", json={
        "username": "deen",
        "password": "secret123",
    })
    assert r.status_code == 200
    data = r.json()
    assert data["username"] == "deen"
    assert "id" in data


def test_register_duplicate_username(app_client):
    app_client.post("/auth/register", json={
        "username": "deen",
        "password": "secret123",
    })
    r = app_client.post("/auth/register", json={
        "username": "deen",
        "password": "otherpass",
    })
    assert r.status_code == 400


def test_login_success(app_client):
    app_client.post("/auth/register", json={
        "username": "deen",
        "password": "secret123",
    })
    r = app_client.post("/auth/login", json={
        "username": "deen",
        "password": "secret123",
    })
    assert r.status_code == 200
    data = r.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"


def test_login_wrong_password(app_client):
    app_client.post("/auth/register", json={
        "username": "deen",
        "password": "secret123",
    })
    r = app_client.post("/auth/login", json={
        "username": "deen",
        "password": "wrongpass",
    })
    assert r.status_code == 401


def test_login_nonexistent_user(app_client):
    r = app_client.post("/auth/login", json={
        "username": "nobody",
        "password": "secret123",
    })
    assert r.status_code == 401
