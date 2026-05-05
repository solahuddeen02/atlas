from __future__ import annotations
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import ForeignKey, Integer, Text, Boolean


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    created_at: Mapped[str | None] = mapped_column(Text, nullable=True)


class TenantMember(Base):
    __tablename__ = "tenant_members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(Integer, ForeignKey("tenants.id"), nullable=False)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False, default="member")
    # role: owner | admin | member


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    hashed_password: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class ShareLink(Base):
    __tablename__ = "share_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    object_id: Mapped[int] = mapped_column(Integer, ForeignKey("objects.id"), nullable=False)
    tenant_id: Mapped[int] = mapped_column(Integer, ForeignKey("tenants.id"), nullable=False)
    created_by: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    expires_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class Object(Base):
    __tablename__ = "objects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("tenants.id"), nullable=True)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    parent_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mime_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    deleted_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="ready")
