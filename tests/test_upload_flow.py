def test_upload_list_download_roundtrip(app_client, test_paths):
    # Create a small file in temp to upload
    content = b"hello-atlas"
    file_path = test_paths["data_dir"].parent / "hello.txt"
    file_path.write_bytes(content)

    # Upload
    with open(file_path, "rb") as f:
        r = app_client.post(
            "/objects/upload?obj_type=file&name=hello.txt",
            files={"file": ("hello.txt", f, "text/plain")},
        )

    assert r.status_code == 200, r.text
    payload = r.json()
    assert "id" in payload
    assert payload["name"] == "hello.txt"
    assert payload["mime_type"] == "text/plain"
    assert payload["status"] == "ready"
    assert isinstance(payload["size"], int)
    assert payload["size"] == len(content)

    obj_id = payload["id"]

    # List should include it
    r2 = app_client.get("/objects?limit=10&offset=0")
    assert r2.status_code == 200
    items = r2.json()
    assert any(x["id"] == obj_id and x["status"] == "ready" for x in items)

    # Download should match content
    r3 = app_client.get(f"/objects/{obj_id}/download")
    assert r3.status_code == 200
    assert r3.content == content
