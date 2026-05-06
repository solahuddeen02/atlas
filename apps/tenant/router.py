from __future__ import annotations

import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from core.auth.dependencies import CurrentUser, get_current_user
from core.auth.jwt import create_access_token, hash_password
from core.db.models import InviteToken, TenantMember, User
from core.db.session import get_db

router = APIRouter(prefix="/tenant", tags=["tenant"])


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class JoinRequest(BaseModel):
    token: str
    username: str
    password: str


@router.post("/invite")
def create_invite(db: Session = Depends(get_db), current_user: CurrentUser = Depends(get_current_user)):
    if current_user.role not in ("owner", "admin"):
        raise HTTPException(status_code=403, detail="admin only")
    token = secrets.token_urlsafe(32)
    invite = InviteToken(
        token=token,
        tenant_id=current_user.tenant_id,
        created_by=current_user.id,
        role="member",
        created_at=_utc_iso(),
    )
    db.add(invite)
    db.commit()
    return {"token": token, "url": f"/join?token={token}"}


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

    existing = db.execute(select(User).where(User.username == body.username)).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail="username already taken")

    now = _utc_iso()
    user = User(username=body.username, hashed_password=hash_password(body.password), created_at=now)
    db.add(user)
    db.flush()

    member = TenantMember(tenant_id=invite.tenant_id, user_id=user.id, role=invite.role)
    db.add(member)

    db.execute(update(InviteToken).where(InviteToken.id == invite.id).values(used_at=now))
    db.commit()

    access_token = create_access_token(user.id, user.username, invite.tenant_id, invite.role)
    return {"access_token": access_token, "token_type": "bearer"}
