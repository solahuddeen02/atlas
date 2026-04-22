# core/apps/photos/router.py
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from core.db.session import get_db
from core.domain.objects import list_photos

router = APIRouter(tags=["photos"])


@router.get("/photos")
def list_photos_api(
    limit: int = 20,
    offset: int = 0,
    q: str | None = None,
    db: Session = Depends(get_db),
):
    return list_photos(db, limit, offset, q)