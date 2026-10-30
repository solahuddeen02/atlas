def test_create_folder(app_client, auth_headers, create_folder):
    r = create_folder(auth_headers, "my-folder")
    assert r.status_code == 200
    data = r.json()
    assert data["name"] == "my-folder"
    assert data["type"] == "folder"


def test_create_nested_folder(app_client, auth_headers, create_folder):
    parent_id = create_folder(auth_headers, "parent").json()["id"]

    r2 = create_folder(auth_headers, "child", parent_id=parent_id)
    assert r2.status_code == 200
    assert r2.json()["parent_id"] == parent_id


def test_list_folder_root(app_client, auth_headers, create_folder):
    create_folder(auth_headers, "root-folder")
    r = app_client.get("/drive/root", headers=auth_headers)
    assert r.status_code == 200
    assert [item["name"] for item in r.json()] == ["root-folder"]


def test_list_folder_by_id(app_client, auth_headers, create_folder):
    parent_id = create_folder(auth_headers, "parent").json()["id"]
    create_folder(auth_headers, "child", parent_id=parent_id)

    r2 = app_client.get(f"/folders/{parent_id}", headers=auth_headers)
    assert r2.status_code == 200
    names = [item["name"] for item in r2.json()]
    assert "child" in names
