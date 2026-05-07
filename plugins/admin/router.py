from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from core.auth.dependencies import CurrentUser, get_current_user
from core.db.models import Object, TenantMember
from core.db.session import get_db

router = APIRouter(prefix="/admin", tags=["admin"])


def _require_admin(current_user: CurrentUser) -> None:
    if current_user.role not in ("owner", "admin"):
        raise HTTPException(status_code=403, detail="admin only")


@router.get("/stats")
def get_stats(db: Session = Depends(get_db), current_user: CurrentUser = Depends(get_current_user)):
    _require_admin(current_user)
    tid = current_user.tenant_id

    members = db.execute(
        select(func.count()).select_from(TenantMember).where(TenantMember.tenant_id == tid)
    ).scalar() or 0

    type_rows = db.execute(
        select(Object.type, func.count(), func.coalesce(func.sum(Object.size), 0))
        .where(and_(Object.tenant_id == tid, Object.deleted_at.is_(None), Object.status == "ready"))
        .group_by(Object.type)
    ).all()

    counts = {row[0]: {"count": row[1], "size": row[2]} for row in type_rows}
    storage_used = sum(r[2] for r in type_rows)

    trashed = db.execute(
        select(func.count()).select_from(Object)
        .where(and_(Object.tenant_id == tid, Object.deleted_at.is_not(None)))
    ).scalar() or 0

    return {
        "members": members,
        "files": counts.get("file", {}).get("count", 0),
        "folders": counts.get("folder", {}).get("count", 0),
        "photos": counts.get("photo", {}).get("count", 0),
        "storage_used": storage_used,
        "trashed": trashed,
    }
