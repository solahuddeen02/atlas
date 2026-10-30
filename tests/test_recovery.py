import os

from core.domain.objects import recover_incomplete_uploads
from core.db.models import Object


def test_startup_recovery_marks_uploading_failed_and_deletes_file(test_paths, db_session):
    """
    Simulate a crash scenario:
    - DB has object with status='uploading'
    - a file exists on disk at data/<id>
    Recovery should:
    - delete the file
    - mark status='failed'
    - clear storage_key/size/mime_type
    """
    data_dir = str(test_paths["data_dir"])

    db = db_session
    try:
        obj = Object(
            type="file",
            name="stuck.bin",
            parent_id=None,
            owner_id=1,
            created_at="2026-01-01T00:00:00",
            status="uploading",
            storage_key=None,
            size=123,
            mime_type="application/octet-stream",
        )
        db.add(obj)
        db.commit()
        db.refresh(obj)
        obj_id = obj.id

        # Create an orphan/incomplete file at the expected path: data/<id>
        stuck_path = os.path.join(data_dir, str(obj_id))
        with open(stuck_path, "wb") as f:
            f.write(b"incomplete")

        assert os.path.exists(stuck_path)

        # Run recovery
        stats = recover_incomplete_uploads(db, data_dir=data_dir)
        assert stats["recovered"] >= 1

        # File should be deleted
        assert not os.path.exists(stuck_path)

        # DB row should be marked failed and cleared
        refreshed = db.get(Object, obj_id)
        assert refreshed is not None
        assert refreshed.status == "failed"
        assert refreshed.storage_key is None
        assert refreshed.size is None
        assert refreshed.mime_type is None

    finally:
        db.close()


def test_recovery_deletes_tmp_files(test_paths, db_session):
    """
    If tmp files like '.<id>.tmp' exist, recovery should delete them.
    """
    data_dir = test_paths["data_dir"]

    # Create a fake tmp file
    tmp_file = data_dir / ".999.tmp"
    tmp_file.write_bytes(b"tmp")

    assert tmp_file.exists()

    db = db_session
    try:
        stats = recover_incomplete_uploads(db, data_dir=str(data_dir))
        assert stats["deleted_tmp"] >= 1
        assert not tmp_file.exists()
    finally:
        db.close()
