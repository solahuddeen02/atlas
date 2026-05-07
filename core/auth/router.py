from __future__ import annotations

import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.auth.dependencies import get_current_user, CurrentUser
from core.auth.jwt import create_access_token, hash_password, verify_password
from core.config import COOKIE_DOMAIN, COOKIE_MAX_AGE, COOKIE_NAME, COOKIE_SECURE
from core.db.models import Tenant, TenantMember, User
from core.db.session import get_db

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    username: str
    password: str
    tenant_name: str | None = None


class LoginRequest(BaseModel):
    username: str
    password: str


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _set_auth_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        domain=COOKIE_DOMAIN,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
        max_age=COOKIE_MAX_AGE,
        path="/",
    )


@router.post("/register")
def register(body: RegisterRequest, db: Session = Depends(get_db)):
    existing = db.execute(
        select(User).where(User.username == body.username)
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail="username already taken")

    now = datetime.now(timezone.utc).isoformat()

    user = User(username=body.username, hashed_password=hash_password(body.password), created_at=now)
    db.add(user)
    db.flush()

    tenant_name = body.tenant_name or body.username
    slug = _slugify(tenant_name)
    existing_slug = db.execute(select(Tenant).where(Tenant.slug == slug)).scalar_one_or_none()
    if existing_slug:
        slug = f"{slug}-{user.id}"

    tenant = Tenant(name=tenant_name, slug=slug, created_at=now)
    db.add(tenant)
    db.flush()

    member = TenantMember(tenant_id=tenant.id, user_id=user.id, role="owner")
    db.add(member)
    db.commit()

    return {"id": user.id, "username": user.username, "tenant_id": tenant.id, "tenant_slug": tenant.slug}


@router.post("/login")
def login(body: LoginRequest, response: Response, db: Session = Depends(get_db)):
    user = db.execute(
        select(User).where(User.username == body.username)
    ).scalar_one_or_none()

    if not user or not verify_password(body.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="invalid credentials")

    member = db.execute(
        select(TenantMember).where(TenantMember.user_id == user.id)
    ).scalar_one_or_none()

    if not member:
        raise HTTPException(status_code=403, detail="user has no tenant membership")

    token = create_access_token(user.id, user.username, member.tenant_id, member.role)
    _set_auth_cookie(response, token)
    return {"access_token": token, "token_type": "bearer"}


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(key=COOKIE_NAME, domain=COOKIE_DOMAIN, path="/")
    return {"status": "ok"}


@router.get("/me")
def me(current_user: CurrentUser = Depends(get_current_user)):
    return {
        "id":        current_user.id,
        "username":  current_user.username,
        "tenant_id": current_user.tenant_id,
        "role":      current_user.role,
    }
