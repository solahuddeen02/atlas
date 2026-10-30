def test_upload_list_download_roundtrip(app_client, auth_headers, upload):
    content = b"hello-atlas"

    r = upload(auth_headers, "hello.txt", content)
    assert r.status_code == 200, r.text
    payload = r.json()
    assert "id" in payload
    assert payload["name"] == "hello.txt"
    assert payload["mime_type"] == "text/plain"
    assert payload["status"] == "ready"
    assert payload["size"] == len(content)

    obj_id = payload["id"]

    r2 = app_client.get("/objects?limit=10&offset=0", headers=auth_headers)
    assert r2.status_code == 200
    assert any(x["id"] == obj_id and x["status"] == "ready" for x in r2.json())

    r3 = app_client.get(f"/objects/{obj_id}/download", headers=auth_headers)
    assert r3.status_code == 200
    assert r3.content == content
