from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from core.auth.dependencies import CurrentUser, get_current_user
from core.auth.jwt import create_access_token, hash_password
from core.db.models import InviteToken, TenantMember, User
from core.db.session import get_db

router = APIRouter(prefix="/tenant", tags=["tenant"])


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_admin(current_user: CurrentUser) -> None:
    if current_user.role not in ("owner", "admin"):
        raise HTTPException(status_code=403, detail="admin only")


# ── Invites ───────────────────────────────────────────────────────────────────

class InviteRequest(BaseModel):
    role: str = "member"
    expires_in_days: int | None = None


@router.post("/invite")
def create_invite(
    body: InviteRequest = InviteRequest(),
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    _require_admin(current_user)
    if body.role not in ("member", "admin", "owner"):
        raise HTTPException(status_code=400, detail="invalid role")
    if body.role == "owner" and current_user.role != "owner":
        raise HTTPException(status_code=403, detail="only owner can invite as owner")

    now = datetime.now(timezone.utc)
    expires_at = (now + timedelta(days=body.expires_in_days)).isoformat() if body.expires_in_days else None

    token = secrets.token_urlsafe(32)
    db.add(InviteToken(
        token=token,
        tenant_id=current_user.tenant_id,
        created_by=current_user.id,
        role=body.role,
        expires_at=expires_at,
        created_at=now.isoformat(),
    ))
    db.commit()
    return {"token": token, "role": body.role, "expires_at": expires_at, "url": f"/join?token={token}"}


@router.get("/invites")
def list_invites(
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    _require_admin(current_user)
    rows = db.execute(
        select(InviteToken)
        .where(InviteToken.tenant_id == current_user.tenant_id, InviteToken.used_at.is_(None))
        .order_by(InviteToken.created_at.desc())
    ).scalars().all()
    return [
        {"token": r.token, "role": r.role, "created_at": r.created_at, "expires_at": r.expires_at}
        for r in rows
    ]


@router.delete("/invites/{token}")
def revoke_invite(
    token: str,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    _require_admin(current_user)
    existing = db.execute(
        select(InviteToken).where(
            InviteToken.token == token,
            InviteToken.tenant_id == current_user.tenant_id,
        )
    ).scalar_one_or_none()
    if not existing:
        raise HTTPException(status_code=404, detail="invite not found")
    db.execute(delete(InviteToken).where(InviteToken.token == token))
    db.commit()
    return {"token": token, "status": "revoked"}


# ── Join ──────────────────────────────────────────────────────────────────────

class JoinRequest(BaseModel):
    token: str
    username: str
    password: str


@router.post("/join")
def join_tenant(body: JoinRequest, db: Session = Depends(get_db)):
    invite = db.execute(
        select(InviteToken).where(InviteToken.token == body.token)
    ).scalar_one_or_none()

    if not invite:
        raise HTTPException(status_code=404, detail="invalid invite token")
    if invite.used_at:
        raise HTTPException(status_code=400, detail="invite already used")
    if invite.expires_at and invite.expires_at < _utc_iso():
        raise HTTPException(status_code=400, detail="invite expired")

    if db.execute(select(User).where(User.username == body.username)).scalar_one_or_none():
        raise HTTPException(status_code=400, detail="username already taken")

    now = _utc_iso()
    user = User(username=body.username, hashed_password=hash_password(body.password), created_at=now)
    db.add(user)
    db.flush()

    db.add(TenantMember(tenant_id=invite.tenant_id, user_id=user.id, role=invite.role))
    db.execute(update(InviteToken).where(InviteToken.id == invite.id).values(used_at=now))
    db.commit()

    access_token = create_access_token(user.id, user.username, invite.tenant_id, invite.role)
    return {"access_token": access_token, "token_type": "bearer"}
