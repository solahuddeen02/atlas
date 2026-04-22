def test_delete_moves_to_trash(app_client, test_paths):
    content = b"trash-me"
    file_path = test_paths["data_dir"].parent / "trash.txt"
    file_path.write_bytes(content)

    with open(file_path, "rb") as f:
        r = app_client.post(
            "/objects/upload?obj_type=file&name=trash.txt",
            files={"file": ("trash.txt", f, "text/plain")},
        )
    obj_id = r.json()["id"]

    # ลบ
    r2 = app_client.delete(f"/objects/{obj_id}")
    assert r2.status_code == 200
    assert r2.json()["status"] == "trashed"

    # ต้องอยู่ใน trash
    r3 = app_client.get("/objects/trash")
    ids = [item["id"] for item in r3.json()]
    assert obj_id in ids

    # ต้องไม่อยู่ใน list ปกติ
    r4 = app_client.get("/objects")
    ids2 = [item["id"] for item in r4.json()]
    assert obj_id not in ids2


def test_restore_from_trash(app_client, test_paths):
    content = b"restore-me"
    file_path = test_paths["data_dir"].parent / "restore.txt"
    file_path.write_bytes(content)

    with open(file_path, "rb") as f:
        r = app_client.post(
            "/objects/upload?obj_type=file&name=restore.txt",
            files={"file": ("restore.txt", f, "text/plain")},
        )
    obj_id = r.json()["id"]

    app_client.delete(f"/objects/{obj_id}")

    r2 = app_client.post(f"/objects/{obj_id}/restore")
    assert r2.status_code == 200
    assert r2.json()["status"] == "restored"

    # ต้องกลับมาใน list ปกติ
    r3 = app_client.get("/objects")
    ids = [item["id"] for item in r3.json()]
    assert obj_id in ids


def test_delete_nonexistent_returns_404(app_client):
    r = app_client.delete("/objects/99999")
    assert r.status_code == 404


def test_restore_nonexistent_returns_404(app_client):
    r = app_client.post("/objects/99999/restore")
    assert r.status_code == 404