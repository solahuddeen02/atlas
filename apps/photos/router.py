from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from core.auth.dependencies import CurrentUser, get_current_user
from core.db.session import get_db
from apps.objects.domain import list_photos

router = APIRouter(tags=["photos"])


@router.get("/photos")
def list_photos_api(limit: int = 20, offset: int = 0, q: str | None = None, db: Session = Depends(get_db), current_user: CurrentUser = Depends(get_current_user)):
    return list_photos(db, limit, offset, q, tenant_id=current_user.tenant_id)
