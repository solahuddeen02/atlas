def _ids(client, url, headers):
    r = client.get(url, headers=headers)
    assert r.status_code == 200, r.text
    return [item["id"] for item in r.json()]


def test_cross_user_parent_id_folder(app_client, two_users, create_folder):
    alice, bob = two_users["alice"], two_users["bob"]
    alice_folder = create_folder(alice, "alice-folder").json()["id"]

    r = create_folder(bob, "evil", parent_id=alice_folder)
    assert r.status_code == 404
    assert _ids(app_client, f"/folders/{alice_folder}", alice) == []
    assert _ids(app_client, "/drive/root", bob) == []


def test_cross_user_parent_id_upload(app_client, two_users, create_folder, upload, test_paths):
    alice, bob = two_users["alice"], two_users["bob"]
    alice_folder = create_folder(alice, "alice-folder").json()["id"]

    r = upload(bob, "evil.txt", b"x", parent_id=alice_folder)
    assert r.status_code == 404
    assert _ids(app_client, f"/folders/{alice_folder}", alice) == []
    assert _ids(app_client, "/objects", bob) == []
    assert list(test_paths["data_dir"].iterdir()) == []


def test_cross_user_parent_id_move(app_client, two_users, create_folder):
    alice, bob = two_users["alice"], two_users["bob"]
    alice_folder = create_folder(alice, "alice-folder").json()["id"]
    bob_folder = create_folder(bob, "bob-folder").json()["id"]

    r = app_client.post(f"/objects/{bob_folder}/move?new_parent_id={alice_folder}", headers=bob)
    assert r.status_code == 404
    assert _ids(app_client, "/drive/root", bob) == [bob_folder]
    assert _ids(app_client, f"/folders/{alice_folder}", alice) == []


def test_parent_must_be_a_live_folder(app_client, auth_headers, create_folder, upload):
    file_id = upload(auth_headers, "file.txt").json()["id"]
    trashed = create_folder(auth_headers, "trashed").json()["id"]
    app_client.delete(f"/objects/{trashed}", headers=auth_headers)

    for parent in (file_id, trashed, 99999):
        assert create_folder(auth_headers, "x", parent_id=parent).status_code == 404
        assert upload(auth_headers, "y.txt", parent_id=parent).status_code == 404


def test_move_cycle_rejected(app_client, auth_headers, create_folder, upload):
    a = create_folder(auth_headers, "a").json()["id"]
    b = create_folder(auth_headers, "b", parent_id=a).json()["id"]
    c = create_folder(auth_headers, "c", parent_id=b).json()["id"]
    file_id = upload(auth_headers, "f.txt").json()["id"]

    def move(obj, target):
        return app_client.post(f"/objects/{obj}/move?new_parent_id={target}", headers=auth_headers)

    assert move(a, a).status_code == 400  # into itself
    assert move(a, c).status_code == 400  # into a descendant
    assert move(a, 99999).status_code == 404  # nonexistent target
    assert move(a, file_id).status_code == 404  # target is not a folder
    assert _ids(app_client, "/drive/root", auth_headers).count(a) == 1  # unchanged

    assert move(c, a).status_code == 200  # a legitimate move still works


def test_other_users_object_is_404_not_403(app_client, two_users, upload):
    alice, bob = two_users["alice"], two_users["bob"]
    file_id = upload(alice, "secret.txt", b"alice-only").json()["id"]

    assert app_client.get(f"/objects/{file_id}/download", headers=bob).status_code == 404
    assert app_client.patch(f"/objects/{file_id}/rename", json={"name": "x"}, headers=bob).status_code == 404
    assert app_client.delete(f"/objects/{file_id}", headers=bob).status_code == 404
    assert app_client.post(f"/objects/{file_id}/restore", headers=bob).status_code == 404
    assert app_client.post(f"/objects/{file_id}/move", headers=bob).status_code == 404
    assert app_client.delete(f"/objects/{file_id}/permanent", headers=bob).status_code == 404
    assert app_client.get(f"/objects/{file_id}/download", headers=alice).content == b"alice-only"


def test_permanent_requires_trash(app_client, auth_headers, upload, test_paths):
    file_id = upload(auth_headers, "keep.txt", b"keep").json()["id"]

    r = app_client.delete(f"/objects/{file_id}/permanent", headers=auth_headers)
    assert r.status_code == 409
    assert app_client.get(f"/objects/{file_id}/download", headers=auth_headers).content == b"keep"

    app_client.delete(f"/objects/{file_id}", headers=auth_headers)
    r = app_client.delete(f"/objects/{file_id}/permanent", headers=auth_headers)
    assert r.status_code == 200
    assert list(test_paths["data_dir"].iterdir()) == []


def test_no_storage_path_in_responses(app_client, auth_headers, upload, create_folder, test_paths):
    data_dir = str(test_paths["data_dir"])
    folder = create_folder(auth_headers, "f").json()["id"]
    responses = [
        upload(auth_headers, "a.txt", b"a"),
        upload(auth_headers, "p.png", b"\x89PNG", mime="image/png"),
        upload(auth_headers, "in.txt", b"b", parent_id=folder),
    ]
    trashed = upload(auth_headers, "t.txt", b"t").json()["id"]
    app_client.delete(f"/objects/{trashed}", headers=auth_headers)

    for url in ("/objects", "/photos", "/drive/root", f"/folders/{folder}", "/objects/trash"):
        responses.append(app_client.get(url, headers=auth_headers))

    for r in responses:
        assert r.status_code == 200, r.text
        assert data_dir not in r.text
        body = r.json()
        items = body if isinstance(body, list) else [body]
        assert items, r.request.url
        for item in items:
            assert "storage" not in item
            assert "storage_key" not in item
