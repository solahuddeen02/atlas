# core/apps/drive/router.py
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from core.db.session import get_db
from core.domain.objects import (
    create_folder,
    list_drive_objects,
    list_folder,
)

router = APIRouter(tags=["drive"])


@router.get("/drive")
def list_drive_api(
    limit: int = 20,
    offset: int = 0,
    q: str | None = None,
    db: Session = Depends(get_db),
):
    return list_drive_objects(db, limit, offset, q)


@router.get("/drive/root")
def drive_root(db: Session = Depends(get_db)):
    return list_folder(db, None)


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
def list_folder_api(folder_id: int, db: Session = Depends(get_db)):
    return list_folder(db, folder_id)