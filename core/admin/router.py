from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import and_, delete, func, select
from sqlalchemy.orm import Session

from core.auth.dependencies import CurrentUser, get_current_user
from core.db.models import Object, TenantMember, User
from core.db.session import get_db

router = APIRouter(prefix="/admin", tags=["admin"])


def _require_admin(current_user: CurrentUser) -> None:
    if current_user.role not in ("owner", "admin"):
        raise HTTPException(status_code=403, detail="admin only")


# ── Stats ──────────────────────────────────────────────────────────────────────

@router.get("/stats")
def get_stats(
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
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


# ── Members ───────────────────────────────────────────────────────────────────

@router.get("/members")
def list_members(
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    _require_admin(current_user)
    rows = db.execute(
        select(User.id, User.username, User.created_at, TenantMember.role)
        .join(TenantMember, TenantMember.user_id == User.id)
        .where(TenantMember.tenant_id == current_user.tenant_id)
        .order_by(TenantMember.role, User.username)
    ).all()
    return [
        {"id": r.id, "username": r.username, "role": r.role, "joined_at": r.created_at}
        for r in rows
    ]


class RoleRequest(BaseModel):
    role: str


@router.patch("/members/{user_id}")
def update_member_role(
    user_id: int,
    body: RoleRequest,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    _require_admin(current_user)
    if body.role not in ("member", "admin", "owner"):
        raise HTTPException(status_code=400, detail="invalid role")
    if user_id == current_user.id:
        raise HTTPException(status_code=400, detail="cannot change your own role")
    if body.role == "owner" and current_user.role != "owner":
        raise HTTPException(status_code=403, detail="only owner can grant owner role")

    member = db.execute(
        select(TenantMember).where(
            and_(TenantMember.tenant_id == current_user.tenant_id, TenantMember.user_id == user_id)
        )
    ).scalar_one_or_none()
    if not member:
        raise HTTPException(status_code=404, detail="member not found")

    member.role = body.role
    db.commit()
    return {"user_id": user_id, "role": body.role}


@router.delete("/members/{user_id}")
def remove_member(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    _require_admin(current_user)
    if user_id == current_user.id:
        raise HTTPException(status_code=400, detail="cannot remove yourself")

    existing = db.execute(
        select(TenantMember).where(
            and_(TenantMember.tenant_id == current_user.tenant_id, TenantMember.user_id == user_id)
        )
    ).scalar_one_or_none()
    if not existing:
        raise HTTPException(status_code=404, detail="member not found")

    db.execute(
        delete(TenantMember).where(
            and_(TenantMember.tenant_id == current_user.tenant_id, TenantMember.user_id == user_id)
        )
    )
    db.commit()
    return {"user_id": user_id, "status": "removed"}
