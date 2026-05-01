# core/apps/objects/router.py
from __future__ import annotations

import os
from datetime import datetime

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from core.apps.auth.dependencies import get_current_user
from core.db.models import User

from core.db.session import get_db
from core.domain.objects import (
    attach_metadata,
    attach_storage,
    create_object,
    get_object,
    list_objects,
    list_trash,
    move_object,
    permanent_delete_object,
    rename_object,
    restore_object,
    set_status,
    trash_object_recursive,
)
from core.storage.factory import get_storage

router = APIRouter(prefix="/objects", tags=["objects"])


@router.post("/upload")
def upload_object(
    obj_type: str,
    name: str,
    parent_id: int | None = None,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    path = None
    obj_id: int | None = None
    created_at = datetime.utcnow().isoformat()
    mime_type = file.content_type
    storage = get_storage()  # ← ใช้ factory

    try:
        obj_id = create_object(db, obj_type, name, parent_id, owner_id=current_user.id, commit=False)
        path, size = storage.save(obj_id, file.file)
        attach_storage(db, obj_id, path, commit=False)
        attach_metadata(db, obj_id, size, mime_type, created_at, commit=False)
        set_status(db, obj_id, "ready", commit=False)
        db.commit()

        return {
            "id": obj_id,
            "name": name,
            "size": size,
            "mime_type": mime_type,
            "created_at": created_at,
            "status": "ready",
        }

    except Exception as e:
        db.rollback()
        if path and storage.exists(path): 
            try:
                storage.delete(path)
            except Exception:
                pass
        if obj_id is not None:
            try:
                set_status(db, obj_id, "failed", commit=True)
            except Exception:
                pass
        raise HTTPException(status_code=500, detail=f"upload failed: {type(e).__name__}")


@router.get("/{obj_id}/download")
def download_object(
    obj_id: int, 
    db: Session = Depends(get_db), 
    current_user: User = Depends(get_current_user)
):
    obj = get_object(db, obj_id)
    if not obj:
        raise HTTPException(status_code=404, detail="object not found")

    storage = get_storage()
    if not obj["storage"] or not storage.exists(obj["storage"]):
        raise HTTPException(status_code=404, detail="file not found on storage")

    # local → FileResponse, minio → redirect URL
    backend = os.getenv("STORAGE_BACKEND", "local")
    if backend == "minio":
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url=storage.get_path(obj["storage"]))

    from fastapi.responses import FileResponse
    return FileResponse(
        path=storage.get_path(obj["storage"]),
        filename=obj["name"],
        media_type="application/octet-stream",
    )

@router.get("")
def list_objects_api(
    obj_type: str | None = None,
    limit: int = 20,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    return list_objects(db, obj_type=obj_type, limit=limit, offset=offset, owner_id=current_user.id)


@router.post("/{obj_id}/move")
def move_object_api(
    obj_id: int,
    new_parent_id: int | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    obj = get_object(db, obj_id)
    if not obj:
        raise HTTPException(status_code=404, detail="object not found")
    move_object(db, obj_id, new_parent_id)
    return {"id": obj_id, "new_parent_id": new_parent_id, "status": "moved"}


@router.get("/trash")
def list_trash_api(
    limit: int = 20, 
    offset: int = 0, 
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    return list_trash(db, limit, offset, owner_id=current_user.id)


@router.post("/{obj_id}/restore")
def restore_object_api(
    obj_id: int, 
    db: Session = Depends(get_db), 
    current_user: User = Depends(get_current_user)
):
    obj = get_object(db, obj_id)
    if not obj:
        raise HTTPException(status_code=404, detail="object not found")
    restore_object(db, obj_id)
    return {"id": obj_id, "status": "restored"}


@router.patch("/{obj_id}/rename")
def rename_object_api(
    obj_id: int,
    name: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    obj = get_object(db, obj_id)
    if not obj:
        raise HTTPException(status_code=404, detail="object not found")
    rename_object(db, obj_id, name)
    return {"id": obj_id, "name": name}


@router.delete("/{obj_id}")
def delete_object(
    obj_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    obj = get_object(db, obj_id)
    if not obj:
        raise HTTPException(status_code=404, detail="object not found")
    trash_object_recursive(db, obj_id)
    return {"status": "trashed", "id": obj_id}


@router.delete("/{obj_id}/permanent")
def permanent_delete_object_api(
    obj_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    obj = get_object(db, obj_id)
    if not obj:
        raise HTTPException(status_code=404, detail="object not found")

    storage_key = permanent_delete_object(db, obj_id)

    if storage_key:
        storage = get_storage()
        if storage.exists(storage_key):
            try:
                storage.delete(storage_key)
            except Exception:
                pass

    return {"status": "deleted", "id": obj_id}