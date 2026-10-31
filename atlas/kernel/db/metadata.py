"""Kernel tables (ARCHITECTURE §7, ADR-002, ADR-005).

These definitions are what `alembic check` compares the migrated schema against.
Migrations are frozen copies: never import this module from a migration.

Conventions: schema `kernel`; every timestamp is `timestamptz` (UTC); every `*_id`
column has a foreign key and an index; ids exposed in URLs are UUIDv7.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, INET, JSONB, UUID

SCHEMA = "kernel"

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

ROLES = ("owner", "admin", "adult", "child", "guest")
ROLE_CHECK = "role IN ('owner', 'admin', 'adult', 'child', 'guest')"

metadata = MetaData(schema=SCHEMA, naming_convention=NAMING_CONVENTION)


def _ts(name: str, *, nullable: bool = False, default_now: bool = True) -> Column[Any]:
    return Column(
        name,
        DateTime(timezone=True),
        nullable=nullable,
        server_default=func.now() if default_now else None,
    )


def _uuid_pk() -> Column[Any]:
    # Generated in Python with atlas.kernel.db.ids.uuid7().
    return Column("id", UUID(as_uuid=True), primary_key=True)


def _user_fk(name: str, *, nullable: bool = True, ondelete: str | None = None) -> Column[Any]:
    return Column(
        name,
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete=ondelete),
        nullable=nullable,
        index=True,
    )


users = Table(
    "users",
    metadata,
    _uuid_pk(),
    # (issuer, sub) is the stable identity key (ADR-001). Both are null until a
    # BYO account link completes.
    Column("idp_issuer", Text),
    Column("idp_sub", Text),
    Column("username", Text, nullable=False),
    Column("display_name", Text, nullable=False, server_default=""),
    Column("role", Text, nullable=False),
    Column("status", Text, nullable=False, server_default="active"),
    _ts("expires_at", nullable=True, default_now=False),
    _ts("created_at"),
    _ts("updated_at"),
    UniqueConstraint("idp_issuer", "idp_sub"),
    UniqueConstraint("username"),
    CheckConstraint(ROLE_CHECK, name="role"),
    CheckConstraint("status IN ('active', 'disabled')", name="status"),
    CheckConstraint("(idp_issuer IS NULL) = (idp_sub IS NULL)", name="identity_pair"),
)

account_links = Table(
    "account_links",
    metadata,
    _uuid_pk(),
    _user_fk("user_id", nullable=False, ondelete="CASCADE"),
    # SHA-256 of the one-time link code; the code itself is never stored.
    Column("code_hash", LargeBinary, nullable=False),
    _ts("expires_at", default_now=False),
    _ts("used_at", nullable=True, default_now=False),
    _user_fk("created_by"),
    _ts("created_at"),
    UniqueConstraint("code_hash"),
)

modules = Table(
    "modules",
    metadata,
    Column("id", Text, primary_key=True),
    Column("enabled", Boolean, nullable=False, server_default=text("false")),
    Column("state", Text, nullable=False, server_default="disabled"),
    Column("installed_version", Text),
    _ts("updated_at"),
    CheckConstraint("id ~ '^[a-z][a-z0-9_]{1,30}$'", name="id_format"),
    CheckConstraint("state IN ('ok', 'misconfigured', 'disabled')", name="state"),
)

permissions = Table(
    "permissions",
    metadata,
    Column("name", Text, primary_key=True),
    Column("module_id", Text, ForeignKey("modules.id"), nullable=False, index=True),
    Column("description", Text, nullable=False, server_default=""),
    Column("step_up", Boolean, nullable=False, server_default=text("false")),
    CheckConstraint("name ~ '^[a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*){1,2}$'", name="name_format"),
)

role_permissions = Table(
    "role_permissions",
    metadata,
    Column("role", Text, nullable=False),
    Column(
        "permission",
        Text,
        ForeignKey("permissions.name", ondelete="CASCADE"),
        nullable=False,
        index=True,
    ),
    PrimaryKeyConstraint("role", "permission"),
    CheckConstraint(ROLE_CHECK, name="role"),
)

user_permission_overrides = Table(
    "user_permission_overrides",
    metadata,
    # Indexed by the primary key's leading column.
    Column("user_id", UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")),
    Column(
        "permission",
        Text,
        ForeignKey("permissions.name", ondelete="CASCADE"),
        nullable=False,
        index=True,
    ),
    Column("effect", Text, nullable=False),
    _user_fk("created_by"),
    _ts("created_at"),
    PrimaryKeyConstraint("user_id", "permission"),
    CheckConstraint("effect IN ('allow', 'deny')", name="effect"),
)

sessions = Table(
    "sessions",
    metadata,
    _uuid_pk(),
    # SHA-256 of the session token; the token itself lives only in the cookie.
    Column("token_hash", LargeBinary, nullable=False),
    _user_fk("user_id", nullable=False, ondelete="CASCADE"),
    _ts("auth_time", default_now=False),
    _ts("created_at"),
    _ts("last_seen_at"),
    _ts("idle_expires_at", default_now=False),
    _ts("absolute_expires_at", default_now=False),
    _ts("revoked_at", nullable=True, default_now=False),
    # The IdP's session id, for back-channel logout. Generic OIDC (`sid` claim).
    Column("idp_sid", Text),
    Column("user_agent", Text),
    Column("client_ip", INET),
    UniqueConstraint("token_hash"),
    Index("ix_sessions_absolute_expires_at", "absolute_expires_at"),
)

audit_log = Table(
    "audit_log",
    metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    _ts("at"),
    _user_fk("actor_user_id"),
    Column("action", Text, nullable=False),
    Column("target_type", Text),
    # Polymorphic: the target may be any table's row, so no FK.
    Column("target_id", Text),
    Column("result", Text, nullable=False),
    # Correlates with logs; not a row reference.
    Column("request_id", Text),
    Column("client_ip", INET),
    Column("details", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    CheckConstraint("result IN ('allowed', 'denied', 'error')", name="result"),
    Index("ix_audit_log_at", "at"),
)

connectors = Table(
    "connectors",
    metadata,
    _uuid_pk(),
    Column("type", Text, nullable=False),
    Column("name", Text, nullable=False),
    # Non-secret settings only. Secrets are referenced by name (ADR-004 §3).
    Column("config", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("secret_refs", ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")),
    Column("enabled", Boolean, nullable=False, server_default=text("false")),
    Column("status", Text, nullable=False, server_default="unknown"),
    _ts("last_checked_at", nullable=True, default_now=False),
    _ts("created_at"),
    _ts("updated_at"),
    UniqueConstraint("name"),
    CheckConstraint(
        "status IN ('unknown', 'healthy', 'unhealthy', 'misconfigured')", name="status"
    ),
)

outbox = Table(
    "outbox",
    metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column("event_type", Text, nullable=False),
    Column("payload", JSONB, nullable=False),
    _ts("created_at"),
    _ts("available_at"),
    Column("attempts", Integer, nullable=False, server_default=text("0")),
    Column("last_error", Text),
    _ts("delivered_at", nullable=True, default_now=False),
    _ts("dead_at", nullable=True, default_now=False),
    Index(
        "ix_outbox_pending",
        "available_at",
        postgresql_where=text("delivered_at IS NULL AND dead_at IS NULL"),
    ),
)

rate_limits = Table(
    "rate_limits",
    metadata,
    Column("key", Text, nullable=False),
    _ts("window_start", default_now=False),
    Column("count", Integer, nullable=False, server_default=text("0")),
    PrimaryKeyConstraint("key", "window_start"),
)

settings = Table(
    "settings",
    metadata,
    Column("key", Text, primary_key=True),
    Column("value", JSONB, nullable=False),
    _ts("updated_at"),
    _user_fk("updated_by"),
)
