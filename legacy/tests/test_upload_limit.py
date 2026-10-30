from core.db.models import Object

LIMIT = 1024


def _ready_count(db_session):
    db_session.expire_all()
    return db_session.query(Object).filter(Object.status == "ready").count()


def test_upload_size_limit(app_client, auth_headers, upload, test_paths, db_session, monkeypatch):
    monkeypatch.setenv("ATLAS_MAX_UPLOAD_BYTES", str(LIMIT))
    data_dir = test_paths["data_dir"]

    ok = upload(auth_headers, "exact.bin", b"x" * LIMIT, mime="application/octet-stream")
    assert ok.status_code == 200, ok.text
    assert ok.json()["size"] == LIMIT

    # Just over the cap: rejected by the storage layer while streaming
    r = upload(auth_headers, "over.bin", b"x" * (LIMIT + 1), mime="application/octet-stream")
    assert r.status_code == 413
    # Far over the cap: rejected by the middleware before the body is parsed
    r = upload(auth_headers, "huge.bin", b"x" * (LIMIT + 200 * 1024), mime="application/octet-stream")
    assert r.status_code == 413

    assert sorted(p.name for p in data_dir.iterdir()) == [str(ok.json()["id"])]
    assert _ready_count(db_session) == 1
    names = [item["name"] for item in app_client.get("/objects", headers=auth_headers).json()]
    assert names == ["exact.bin"]


def test_upload_limit_without_content_length(app_client, auth_headers, test_paths, db_session, monkeypatch):
    monkeypatch.setenv("ATLAS_MAX_UPLOAD_BYTES", str(LIMIT))

    def chunked_body():
        yield b"--b\r\nContent-Disposition: form-data; name=\"file\"; filename=\"c.bin\"\r\n\r\n"
        for _ in range(100):
            yield b"x" * 4096
        yield b"\r\n--b--\r\n"

    r = app_client.post(
        "/objects/upload?obj_type=file&name=c.bin",
        content=chunked_body(),
        headers={**auth_headers, "Content-Type": "multipart/form-data; boundary=b"},
    )
    assert r.status_code == 413
    assert list(test_paths["data_dir"].iterdir()) == []
    assert _ready_count(db_session) == 0


def test_default_upload_limit_is_100mb(monkeypatch):
    from core.security import get_max_upload_bytes
    monkeypatch.delenv("ATLAS_MAX_UPLOAD_BYTES", raising=False)
    assert get_max_upload_bytes() == 100 * 1024 * 1024
