def test_create_folder(app_client):
    r = app_client.post("/folders?name=my-folder")
    assert r.status_code == 200
    data = r.json()
    assert data["name"] == "my-folder"
    assert data["type"] == "folder"
    assert data["status"] == "ready"


def test_create_nested_folder(app_client):
    r = app_client.post("/folders?name=parent")
    parent_id = r.json()["id"]

    r2 = app_client.post(f"/folders?name=child&parent_id={parent_id}")
    assert r2.status_code == 200
    assert r2.json()["parent_id"] == parent_id


def test_list_folder_root(app_client):
    app_client.post("/folders?name=root-folder")
    r = app_client.get("/drive/root")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_list_folder_by_id(app_client):
    r = app_client.post("/folders?name=parent")
    parent_id = r.json()["id"]
    app_client.post(f"/folders?name=child&parent_id={parent_id}")

    r2 = app_client.get(f"/folders/{parent_id}")
    assert r2.status_code == 200
    names = [item["name"] for item in r2.json()]
    assert "child" in names