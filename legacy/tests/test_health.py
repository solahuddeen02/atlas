def test_health_ok(app_client):
    r = app_client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
