# core/apps/objects/router.py
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
    create_object,
    get_object,
    list_objects,
    list_trash,
    move_object,
    restore_object,
    set_status,
    trash_object_recursive,
)
from core.storage.local import save_file

router = APIRouter(prefix="/objects", tags=["objects"])


@router.post("/upload")
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
        with db.begin():
            obj_id = create_object(db, obj_type, name, parent_id, commit=False)
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
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass
        if obj_id is not None:
            try:
                set_status(db, obj_id, "failed", commit=True)
            except Exception:
                pass
        raise HTTPException(status_code=500, detail=f"upload failed: {type(e).__name__}")


@router.get("/{obj_id}/download")
def download_object(obj_id: int, db: Session = Depends(get_db)):
    obj = get_object(db, obj_id)
    if not obj:
        raise HTTPException(status_code=404, detail="object not found")
    if not obj["storage"] or not os.path.exists(obj["storage"]):
        raise HTTPException(status_code=404, detail="file not found on storage")

    return FileResponse(
        path=obj["storage"],
        filename=obj["name"],
        media_type="application/octet-stream",
    )


@router.get("")
def list_objects_api(
    obj_type: str | None = None,
    limit: int = 20,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    return list_objects(db, obj_type=obj_type, limit=limit, offset=offset)


@router.post("/{obj_id}/move")
def move_object_api(
    obj_id: int,
    new_parent_id: int | None = None,
    db: Session = Depends(get_db),
):
    obj = get_object(db, obj_id)
    if not obj:
        raise HTTPException(status_code=404, detail="object not found")
    move_object(db, obj_id, new_parent_id)
    return {"id": obj_id, "new_parent_id": new_parent_id, "status": "moved"}


@router.get("/trash")
def list_trash_api(limit: int = 20, offset: int = 0, db: Session = Depends(get_db)):
    return list_trash(db, limit, offset)


@router.post("/{obj_id}/restore")
def restore_object_api(obj_id: int, db: Session = Depends(get_db)):
    obj = get_object(db, obj_id)
    if not obj:
        raise HTTPException(status_code=404, detail="object not found")
    restore_object(db, obj_id)
    return {"id": obj_id, "status": "restored"}


@router.delete("/{obj_id}")
def delete_object(obj_id: int, db: Session = Depends(get_db)):
    obj = get_object(db, obj_id)
    if not obj:
        raise HTTPException(status_code=404, detail="object not found")
    trash_object_recursive(db, obj_id)
    return {"status": "trashed", "id": obj_id}