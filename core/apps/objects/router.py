from __future__ import annotations

import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from core.apps.auth.dependencies import CurrentUser, get_current_user
from core.db.session import get_db
from core.apps.objects.schemas import RenameRequest
from core.domain.objects import (
    attach_metadata,
    attach_storage,
    create_object,
    empty_trash,
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


def _get_tenant_object(db: Session, obj_id: int, current_user: CurrentUser) -> dict:
    obj = get_object(db, obj_id)
    if not obj:
        raise HTTPException(status_code=404, detail="object not found")
    if obj["tenant_id"] != current_user.tenant_id:
        raise HTTPException(status_code=403, detail="forbidden")
    return obj


@router.post("/upload")
def upload_object(
    obj_type: str,
    name: str,
    parent_id: int | None = None,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    path = None
    obj_id: int | None = None
    created_at = datetime.now(timezone.utc).isoformat()
    mime_type = file.content_type
    storage = get_storage()

    try:
        obj_id = create_object(
            db, obj_type, name, parent_id,
            owner_id=current_user.id,
            tenant_id=current_user.tenant_id,
            commit=False,
        )
        path, size = storage.save(obj_id, file.file)
        attach_storage(db, obj_id, path, commit=False)
        attach_metadata(db, obj_id, size, mime_type, created_at, commit=False)
        set_status(db, obj_id, "ready", commit=False)
        db.commit()

        return {"id": obj_id, "name": name, "size": size, "mime_type": mime_type, "created_at": created_at, "status": "ready"}

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
    current_user: CurrentUser = Depends(get_current_user),
):
    obj = _get_tenant_object(db, obj_id, current_user)

    storage = get_storage()
    if not obj["storage"] or not storage.exists(obj["storage"]):
        raise HTTPException(status_code=404, detail="file not found on storage")

    if os.getenv("STORAGE_BACKEND", "local") == "minio":
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url=storage.get_path(obj["storage"]))

    return FileResponse(path=storage.get_path(obj["storage"]), filename=obj["name"], media_type="application/octet-stream")


@router.get("")
def list_objects_api(
    obj_type: str | None = None,
    limit: int = 20,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    return list_objects(db, obj_type=obj_type, limit=limit, offset=offset, tenant_id=current_user.tenant_id)


@router.post("/{obj_id}/move")
def move_object_api(
    obj_id: int,
    new_parent_id: int | None = None,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    _get_tenant_object(db, obj_id, current_user)
    move_object(db, obj_id, new_parent_id)
    return {"id": obj_id, "new_parent_id": new_parent_id, "status": "moved"}


@router.get("/trash")
def list_trash_api(
    limit: int = 20,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    return list_trash(db, limit, offset, tenant_id=current_user.tenant_id)


@router.delete("/trash/empty")
def empty_trash_api(
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    storage = get_storage()
    keys = empty_trash(db, current_user.tenant_id)
    for key in keys:
        if storage.exists(key):
            try:
                storage.delete(key)
            except Exception:
                pass
    return {"deleted": len(keys)}


@router.post("/{obj_id}/restore")
def restore_object_api(
    obj_id: int,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    _get_tenant_object(db, obj_id, current_user)
    restore_object(db, obj_id)
    return {"id": obj_id, "status": "restored"}


@router.patch("/{obj_id}/rename")
def rename_object_api(
    obj_id: int,
    body: RenameRequest,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    _get_tenant_object(db, obj_id, current_user)
    rename_object(db, obj_id, body.name)
    return {"id": obj_id, "name": body.name}


@router.delete("/{obj_id}")
def delete_object(
    obj_id: int,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    _get_tenant_object(db, obj_id, current_user)
    trash_object_recursive(db, obj_id)
    return {"status": "trashed", "id": obj_id}


@router.delete("/{obj_id}/permanent")
def permanent_delete_object_api(
    obj_id: int,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    _get_tenant_object(db, obj_id, current_user)
    storage_key = permanent_delete_object(db, obj_id)

    if storage_key:
        storage = get_storage()
        if storage.exists(storage_key):
            try:
                storage.delete(storage_key)
            except Exception:
                pass

    return {"status": "deleted", "id": obj_id}
