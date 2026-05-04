def test_move_object_to_folder(app_client, test_paths):
    # สร้าง folder
    r = app_client.post("/folders?name=destination")
    folder_id = r.json()["id"]

    # upload file
    content = b"move-me"
    file_path = test_paths["data_dir"].parent / "move.txt"
    file_path.write_bytes(content)

    with open(file_path, "rb") as f:
        r2 = app_client.post(
            "/objects/upload?obj_type=file&name=move.txt",
            files={"file": ("move.txt", f, "text/plain")},
        )
    obj_id = r2.json()["id"]

    # move
    r3 = app_client.post(f"/objects/{obj_id}/move?new_parent_id={folder_id}")
    assert r3.status_code == 200
    assert r3.json()["new_parent_id"] == folder_id

    # ต้องอยู่ใน folder นั้น
    r4 = app_client.get(f"/folders/{folder_id}")
    ids = [item["id"] for item in r4.json()]
    assert obj_id in ids


def test_move_nonexistent_returns_404(app_client):
    r = app_client.post("/objects/99999/move?new_parent_id=1")
    assert r.status_code == 404