# core/apps/drive/router.py
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from core.apps.auth.dependencies import get_current_user
from core.db.models import User
from core.db.session import get_db
from core.domain.objects import create_folder, list_folder

router = APIRouter(tags=["drive"])


@router.get("/drive/root")
def drive_root(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return list_folder(db, None, owner_id=current_user.id)


@router.post("/folders")
def create_folder_api(
    name: str,
    parent_id: int | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    folder_id = create_folder(db, name, parent_id, owner_id=current_user.id)
    return {"id": folder_id, "name": name, "parent_id": parent_id, "type": "folder"}


@router.get("/folders/{folder_id}")
def list_folder_api(
    folder_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return list_folder(db, folder_id, owner_id=current_user.id)
