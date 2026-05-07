from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.auth.jwt import decode_access_token
from core.config import COOKIE_NAME
from core.db.models import User
from core.db.session import get_db


@dataclass
class CurrentUser:
    id: int
    username: str
    tenant_id: int
    role: str


def _extract_token(request: Request) -> str | None:
    # Bearer header first — for API clients / curl
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    # httpOnly cookie — for browser subdomain auth
    return request.cookies.get(COOKIE_NAME)


def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
) -> CurrentUser:
    token = _extract_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="not authenticated")

    payload = decode_access_token(token)
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
