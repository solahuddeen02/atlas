from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.auth.jwt import decode_access_token
from core.db.models import User
from core.db.session import get_db

bearer = HTTPBearer()


@dataclass
class CurrentUser:
    id: int
    username: str
    tenant_id: int
    role: str


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer),
    db: Session = Depends(get_db),
) -> CurrentUser:
    payload = decode_access_token(credentials.credentials)
    if not payload:
        raise HTTPException(status_code=401, detail="invalid or expired token")

    user = db.execute(
        select(User).where(User.id == int(payload["sub"]))
    ).scalar_one_or_none()

    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="user not found or inactive")

    tenant_id = payload.get("tenant_id")
    if not tenant_id:
        raise HTTPException(status_code=401, detail="token missing tenant context")

    return CurrentUser(
        id=user.id,
        username=user.username,
        tenant_id=tenant_id,
        role=payload.get("role", "member"),
    )
