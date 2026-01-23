# core/api/objects.py
from __future__ import annotations

import os
from datetime import datetime

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from core.db.session import get_db
from core.domain.objects import (
    attach_metadata,
    attach_storage,
    create_folder,
    create_object,
    get_object,
    list_drive_objects,
    list_folder,
    list_objects,
    list_photos,
    list_trash,
    move_object,
    restore_object,
    set_status,
    trash_object_recursive,
)
from core.storage.local import save_file

router = APIRouter()


@router.post("/objects/upload")
def upload_object(
    obj_type: str,
    name: str,
    parent_id: int | None = None,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    path = None
    obj_id: int | None = None

    created_at = datetime.utcnow().isoformat()
    mime_type = file.content_type

    try:
        # One transaction for DB changes.
        with db.begin():
            obj_id = create_object(db, obj_type, name, parent_id, commit=False)

            # Save file (may raise); if it fails we rollback DB and nothing persists
            path, size = save_file(obj_id, file.file)

            attach_storage(db, obj_id, path, commit=False)
            attach_metadata(db, obj_id, size, mime_type, created_at, commit=False)
            set_status(db, obj_id, "ready", commit=False)

        return {
            "id": obj_id,
            "name": name,
            "size": size,
            "mime_type": mime_type,
            "created_at": created_at,
            "status": "ready",
        }

    except Exception as e:
        # db.begin() rolls back automatically on exception.
        # Clean up file if it was written.
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass

        # Best-effort mark failed if obj_id exists and session can still talk
        if obj_id is not None:
            try:
                set_status(db, obj_id, "failed", commit=True)
            except Exception:
                pass

        raise HTTPException(status_code=500, detail=f"upload failed: {type(e).__name__}")


@router.get("/objects/{obj_id}/download")
def download_object(
    obj_id: int,
    db: Session = Depends(get_db),
):
    obj = get_object(db, obj_id)
    if not obj:
        return {"error": "object not found"}

    return FileResponse(
        path=obj["storage"],
        filename=obj["name"],
        media_type="application/octet-stream",
    )


@router.get("/objects")
def list_objects_api(
    obj_type: str | None = None,
    limit: int = 20,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    return list_objects(
        db,
        obj_type=obj_type,
        limit=limit,
        offset=offset,
    )


@router.get("/photos")
def list_photos_api(
    limit: int = 20,
    offset: int = 0,
    q: str | None = None,
    db: Session = Depends(get_db),
):
    return list_photos(db, limit, offset, q)


@router.get("/drive")
def list_drive_api(
    limit: int = 20,
    offset: int = 0,
    q: str | None = None,
    db: Session = Depends(get_db),
):
    return list_drive_objects(db, limit, offset, q)


@router.post("/folders")
def create_folder_api(
    name: str,
    parent_id: int | None = None,
    db: Session = Depends(get_db),
):
    folder_id = create_folder(db, name, parent_id)
    return {
        "id": folder_id,
        "name": name,
        "parent_id": parent_id,
        "type": "folder",
        "status": "ready",
    }


@router.get("/folders/{folder_id}")
def list_folder_api(
    folder_id: int,
    db: Session = Depends(get_db),
):
    return list_folder(db, folder_id)


@router.get("/drive/root")
def drive_root(
    db: Session = Depends(get_db),
):
    return list_folder(db, None)


@router.post("/objects/{obj_id}/move")
def move_object_api(
    obj_id: int,
    new_parent_id: int | None = None,
    db: Session = Depends(get_db),
):
    move_object(db, obj_id, new_parent_id)
    return {
        "id": obj_id,
        "new_parent_id": new_parent_id,
        "status": "moved",
    }


@router.get("/trash")
def list_trash_api(
    limit: int = 20,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    return list_trash(db, limit, offset)


@router.post("/objects/{obj_id}/restore")
def restore_object_api(
    obj_id: int,
    db: Session = Depends(get_db),
):
    restore_object(db, obj_id)
    return {"id": obj_id, "status": "restored"}


@router.delete("/objects/{obj_id}")
def delete_object(
    obj_id: int,
    db: Session = Depends(get_db),
):
    trash_object_recursive(db, obj_id)
    return {"status": "trashed", "id": obj_id}
