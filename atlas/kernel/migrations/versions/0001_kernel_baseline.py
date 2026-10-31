"""Kernel baseline (ADR-005): the kernel tables and the atlas_app grants.

The `kernel` schema itself is created by `atlas-admin db-init`, owned by
atlas_migrator, because Alembic's version table lives in it.

Grants: atlas_app gets DML on every kernel table, except audit_log, which is
append-only for it (INSERT and SELECT). It can't run DDL anywhere.

Revision ID: 0001
Revises:
Create Date: 2026-10-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = ("kernel",)
depends_on: str | Sequence[str] | None = None


APP_ROLE = "atlas_app"


def upgrade() -> None:
    # Tables and sequences the migrator creates in `kernel` from now on are usable by
    # atlas_app. This covers future kernel migrations too.
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA kernel "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {APP_ROLE}"
    )
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA kernel GRANT USAGE, SELECT ON SEQUENCES TO {APP_ROLE}"
    )

    op.create_table(
        "connectors",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "config",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "secret_refs",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("status", sa.Text(), server_default="unknown", nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('unknown', 'healthy', 'unhealthy', 'misconfigured')",
            name=op.f("ck_connectors_status"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_connectors")),
        sa.UniqueConstraint("name", name=op.f("uq_connectors_name")),
        schema="kernel",
    )
    op.create_table(
        "modules",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("state", sa.Text(), server_default="disabled", nullable=False),
        sa.Column("installed_version", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("id ~ '^[a-z][a-z0-9_]{1,30}$'", name=op.f("ck_modules_id_format")),
        sa.CheckConstraint(
            "state IN ('ok', 'misconfigured', 'disabled')", name=op.f("ck_modules_state")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_modules")),
        schema="kernel",
    )
    op.create_table(
        "outbox",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dead_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outbox")),
        schema="kernel",
    )
    op.create_index(
        "ix_outbox_pending",
        "outbox",
        ["available_at"],
        unique=False,
        schema="kernel",
        postgresql_where=sa.text("delivered_at IS NULL AND dead_at IS NULL"),
    )
    op.create_table(
        "rate_limits",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.PrimaryKeyConstraint("key", "window_start", name=op.f("pk_rate_limits")),
        schema="kernel",
    )
    op.create_table(
        "users",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("idp_issuer", sa.Text(), nullable=True),
        sa.Column("idp_sub", sa.Text(), nullable=True),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), server_default="", nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="active", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "role IN ('owner', 'admin', 'adult', 'child', 'guest')", name=op.f("ck_users_role")
        ),
        sa.CheckConstraint("status IN ('active', 'disabled')", name=op.f("ck_users_status")),
        sa.CheckConstraint(
            "(idp_issuer IS NULL) = (idp_sub IS NULL)", name=op.f("ck_users_identity_pair")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("idp_issuer", "idp_sub", name=op.f("uq_users_idp_issuer_idp_sub")),
        sa.UniqueConstraint("username", name=op.f("uq_users_username")),
        schema="kernel",
    )
    op.create_table(
        "account_links",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("code_hash", sa.LargeBinary(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["kernel.users.id"], name=op.f("fk_account_links_created_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["kernel.users.id"],
            name=op.f("fk_account_links_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_account_links")),
        sa.UniqueConstraint("code_hash", name=op.f("uq_account_links_code_hash")),
        schema="kernel",
    )
    op.create_index(
        op.f("ix_account_links_created_by"),
        "account_links",
        ["created_by"],
        unique=False,
        schema="kernel",
    )
    op.create_index(
        op.f("ix_account_links_user_id"),
        "account_links",
        ["user_id"],
        unique=False,
        schema="kernel",
    )
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column(
            "at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("actor_user_id", sa.UUID(), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("target_type", sa.Text(), nullable=True),
        sa.Column("target_id", sa.Text(), nullable=True),
        sa.Column("result", sa.Text(), nullable=False),
        sa.Column("request_id", sa.Text(), nullable=True),
        sa.Column("client_ip", postgresql.INET(), nullable=True),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "result IN ('allowed', 'denied', 'error')", name=op.f("ck_audit_log_result")
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"], ["kernel.users.id"], name=op.f("fk_audit_log_actor_user_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
        schema="kernel",
    )
    op.create_index(
        op.f("ix_audit_log_actor_user_id"),
        "audit_log",
        ["actor_user_id"],
        unique=False,
        schema="kernel",
    )
    op.create_index("ix_audit_log_at", "audit_log", ["at"], unique=False, schema="kernel")
    op.create_table(
        "permissions",
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("module_id", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("step_up", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.CheckConstraint(
            "name ~ '^[a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*){1,2}$'",
            name=op.f("ck_permissions_name_format"),
        ),
        sa.ForeignKeyConstraint(
            ["module_id"], ["kernel.modules.id"], name=op.f("fk_permissions_module_id_modules")
        ),
        sa.PrimaryKeyConstraint("name", name=op.f("pk_permissions")),
        schema="kernel",
    )
    op.create_index(
        op.f("ix_permissions_module_id"),
        "permissions",
        ["module_id"],
        unique=False,
        schema="kernel",
    )
    op.create_table(
        "sessions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("token_hash", sa.LargeBinary(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("auth_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("idle_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("absolute_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("idp_sid", sa.Text(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("client_ip", postgresql.INET(), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["kernel.users.id"],
            name=op.f("fk_sessions_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sessions")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_sessions_token_hash")),
        schema="kernel",
    )
    op.create_index(
        "ix_sessions_absolute_expires_at",
        "sessions",
        ["absolute_expires_at"],
        unique=False,
        schema="kernel",
    )
    op.create_index(
        op.f("ix_sessions_user_id"), "sessions", ["user_id"], unique=False, schema="kernel"
    )
    op.create_table(
        "settings",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_by", sa.UUID(), nullable=True),
        sa.ForeignKeyConstraint(
            ["updated_by"], ["kernel.users.id"], name=op.f("fk_settings_updated_by_users")
        ),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_settings")),
        schema="kernel",
    )
    op.create_index(
        op.f("ix_settings_updated_by"), "settings", ["updated_by"], unique=False, schema="kernel"
    )
    op.create_table(
        "role_permissions",
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("permission", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "role IN ('owner', 'admin', 'adult', 'child', 'guest')",
            name=op.f("ck_role_permissions_role"),
        ),
        sa.ForeignKeyConstraint(
            ["permission"],
            ["kernel.permissions.name"],
            name=op.f("fk_role_permissions_permission_permissions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("role", "permission", name=op.f("pk_role_permissions")),
        schema="kernel",
    )
    op.create_index(
        op.f("ix_role_permissions_permission"),
        "role_permissions",
        ["permission"],
        unique=False,
        schema="kernel",
    )
    op.create_table(
        "user_permission_overrides",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("permission", sa.Text(), nullable=False),
        sa.Column("effect", sa.Text(), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "effect IN ('allow', 'deny')", name=op.f("ck_user_permission_overrides_effect")
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["kernel.users.id"],
            name=op.f("fk_user_permission_overrides_created_by_users"),
        ),
        sa.ForeignKeyConstraint(
            ["permission"],
            ["kernel.permissions.name"],
            name=op.f("fk_user_permission_overrides_permission_permissions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["kernel.users.id"],
            name=op.f("fk_user_permission_overrides_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "permission", name=op.f("pk_user_permission_overrides")),
        schema="kernel",
    )
    op.create_index(
        op.f("ix_user_permission_overrides_created_by"),
        "user_permission_overrides",
        ["created_by"],
        unique=False,
        schema="kernel",
    )
    op.create_index(
        op.f("ix_user_permission_overrides_permission"),
        "user_permission_overrides",
        ["permission"],
        unique=False,
        schema="kernel",
    )

    # The audit trail is append-only for the app (ADR-002 rule 7, ADR-005 §1).
    op.execute(f"REVOKE UPDATE, DELETE, TRUNCATE ON kernel.audit_log FROM {APP_ROLE}")


def downgrade() -> None:
    # Development only: production migrations are forward-only (ADR-005 §2).
    op.drop_index(
        op.f("ix_user_permission_overrides_permission"),
        table_name="user_permission_overrides",
        schema="kernel",
    )
    op.drop_index(
        op.f("ix_user_permission_overrides_created_by"),
        table_name="user_permission_overrides",
        schema="kernel",
    )
    op.drop_table("user_permission_overrides", schema="kernel")
    op.drop_index(
        op.f("ix_role_permissions_permission"), table_name="role_permissions", schema="kernel"
    )
    op.drop_table("role_permissions", schema="kernel")
    op.drop_index(op.f("ix_settings_updated_by"), table_name="settings", schema="kernel")
    op.drop_table("settings", schema="kernel")
    op.drop_index(op.f("ix_sessions_user_id"), table_name="sessions", schema="kernel")
    op.drop_index("ix_sessions_absolute_expires_at", table_name="sessions", schema="kernel")
    op.drop_table("sessions", schema="kernel")
    op.drop_index(op.f("ix_permissions_module_id"), table_name="permissions", schema="kernel")
    op.drop_table("permissions", schema="kernel")
    op.drop_index("ix_audit_log_at", table_name="audit_log", schema="kernel")
    op.drop_index(op.f("ix_audit_log_actor_user_id"), table_name="audit_log", schema="kernel")
    op.drop_table("audit_log", schema="kernel")
    op.drop_index(op.f("ix_account_links_user_id"), table_name="account_links", schema="kernel")
    op.drop_index(op.f("ix_account_links_created_by"), table_name="account_links", schema="kernel")
    op.drop_table("account_links", schema="kernel")
    op.drop_table("users", schema="kernel")
    op.drop_table("rate_limits", schema="kernel")
    op.drop_index(
        "ix_outbox_pending",
        table_name="outbox",
        schema="kernel",
        postgresql_where=sa.text("delivered_at IS NULL AND dead_at IS NULL"),
    )
    op.drop_table("outbox", schema="kernel")
    op.drop_table("modules", schema="kernel")
    op.drop_table("connectors", schema="kernel")
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA kernel "
        f"REVOKE USAGE, SELECT ON SEQUENCES FROM {APP_ROLE}"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA kernel "
        f"REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {APP_ROLE}"
    )
