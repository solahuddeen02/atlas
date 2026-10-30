def test_move_object_to_folder(app_client, auth_headers, upload, create_folder):
    folder_id = create_folder(auth_headers, "destination").json()["id"]
    obj_id = upload(auth_headers, "move.txt", b"move-me").json()["id"]

    r3 = app_client.post(f"/objects/{obj_id}/move?new_parent_id={folder_id}", headers=auth_headers)
    assert r3.status_code == 200
    assert r3.json()["new_parent_id"] == folder_id

    r4 = app_client.get(f"/folders/{folder_id}", headers=auth_headers)
    ids = [item["id"] for item in r4.json()]
    assert obj_id in ids


def test_move_to_root(app_client, auth_headers, create_folder):
    parent_id = create_folder(auth_headers, "parent").json()["id"]
    child_id = create_folder(auth_headers, "child", parent_id=parent_id).json()["id"]

    r = app_client.post(f"/objects/{child_id}/move", headers=auth_headers)
    assert r.status_code == 200
    root_ids = [item["id"] for item in app_client.get("/drive/root", headers=auth_headers).json()]
    assert child_id in root_ids


def test_move_nonexistent_returns_404(app_client, auth_headers):
    r = app_client.post("/objects/99999/move?new_parent_id=1", headers=auth_headers)
    assert r.status_code == 404
