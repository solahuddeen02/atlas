def test_create_folder(auth_client):
    r = auth_client.post("/folders", json={"name": "my-folder"})
    assert r.status_code == 200
    data = r.json()
    assert data["name"] == "my-folder"
    assert data["type"] == "folder"


def test_create_nested_folder(auth_client):
    r = auth_client.post("/folders", json={"name": "parent"})
    parent_id = r.json()["id"]

    r2 = auth_client.post("/folders", json={"name": "child", "parent_id": parent_id})
    assert r2.status_code == 200
    assert r2.json()["parent_id"] == parent_id


def test_list_folder_root(auth_client):
    auth_client.post("/folders", json={"name": "root-folder"})
    r = auth_client.get("/drive/root")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_list_folder_by_id(auth_client):
    r = auth_client.post("/folders", json={"name": "parent"})
    parent_id = r.json()["id"]
    auth_client.post("/folders", json={"name": "child", "parent_id": parent_id})

    r2 = auth_client.get(f"/folders/{parent_id}")
    assert r2.status_code == 200
    names = [item["name"] for item in r2.json()]
    assert "child" in names
