def test_delete_moves_to_trash(app_client, auth_headers, upload):
    obj_id = upload(auth_headers, "trash.txt", b"trash-me").json()["id"]

    r2 = app_client.delete(f"/objects/{obj_id}", headers=auth_headers)
    assert r2.status_code == 200
    assert r2.json()["status"] == "trashed"

    r3 = app_client.get("/objects/trash", headers=auth_headers)
    assert obj_id in [item["id"] for item in r3.json()]

    r4 = app_client.get("/objects", headers=auth_headers)
    assert obj_id not in [item["id"] for item in r4.json()]


def test_restore_from_trash(app_client, auth_headers, upload):
    obj_id = upload(auth_headers, "restore.txt", b"restore-me").json()["id"]
    app_client.delete(f"/objects/{obj_id}", headers=auth_headers)

    r2 = app_client.post(f"/objects/{obj_id}/restore", headers=auth_headers)
    assert r2.status_code == 200
    assert r2.json()["status"] == "restored"

    r3 = app_client.get("/objects", headers=auth_headers)
    assert obj_id in [item["id"] for item in r3.json()]


def test_delete_nonexistent_returns_404(app_client, auth_headers):
    r = app_client.delete("/objects/99999", headers=auth_headers)
    assert r.status_code == 404


def test_restore_nonexistent_returns_404(app_client, auth_headers):
    r = app_client.post("/objects/99999/restore", headers=auth_headers)
    assert r.status_code == 404


def test_requires_auth(app_client):
    r = app_client.get("/objects")
    assert r.status_code in (401, 403)
