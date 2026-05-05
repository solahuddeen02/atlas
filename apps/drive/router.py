from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from core.auth.dependencies import CurrentUser, get_current_user
from core.db.session import get_db
from apps.drive.schemas import CreateFolderRequest
from apps.objects.domain import create_folder, list_folder

router = APIRouter(tags=["drive"])


@router.get("/drive/root")
def drive_root(limit: int = 20, offset: int = 0, db: Session = Depends(get_db), current_user: CurrentUser = Depends(get_current_user)):
    return list_folder(db, None, tenant_id=current_user.tenant_id, limit=limit, offset=offset)


@router.post("/folders")
def create_folder_api(body: CreateFolderRequest, db: Session = Depends(get_db), current_user: CurrentUser = Depends(get_current_user)):
    folder_id = create_folder(db, body.name, body.parent_id, owner_id=current_user.id, tenant_id=current_user.tenant_id)
    return {"id": folder_id, "name": body.name, "parent_id": body.parent_id, "type": "folder"}


@router.get("/folders/{folder_id}")
def list_folder_api(folder_id: int, limit: int = 20, offset: int = 0, db: Session = Depends(get_db), current_user: CurrentUser = Depends(get_current_user)):
    return list_folder(db, folder_id, tenant_id=current_user.tenant_id, limit=limit, offset=offset)
