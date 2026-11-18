"""Sessions for the OIDC relying party (Phase 5c-1, ADR-003 §4).

`kernel.sessions` has existed empty since the baseline and nothing has written to it, so
no session can exist before this revision: it clears the table (a no-op everywhere) and
then adds what the session store needs.

- `csrf_token`: the per-session synchronizer token, NOT NULL. The store always writes
  one; the database never invents a value of its own. (The table is empty, so the column
  can be added without a default.)
- `idp_tokens_enc`: the IdP refresh token, encrypted by the application (AES-GCM, the
  row's id as associated data). Cleared when it is taken to be revoked at the IdP, and
  deleted with the row.
- `revalidate_after`: when the next IdP re-validation is due.
- `revoked_reason`: why `revoked_at` is set. The two are set together or not at all.
- a partial index for "this user's live sessions" (revoke-all, listing).

`kernel.login_attempts` holds the logins in flight: one row per login or step-up, consumed
by a single `DELETE ... RETURNING`, so a `state` and a callback can be used once. The app
role may insert, select and delete there, and never update.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "atlas_app"

REVOKE_REASONS = (
    "logout",
    "user",
    "admin",
    "disabled",
    "role_changed",
    "expired",
    "idp_ended",
    "idp_unreachable",
    "replaced",
)


def upgrade() -> None:
    # Nothing has ever created a session (the table is unused before 5c), so this deletes
    # no row. It is here so that the NOT NULL column below can never meet an old one.
    op.execute("DELETE FROM kernel.sessions")

    op.add_column("sessions", sa.Column("csrf_token", sa.Text(), nullable=False), schema="kernel")
    op.add_column(
        "sessions", sa.Column("idp_tokens_enc", sa.LargeBinary(), nullable=True), schema="kernel"
    )
    op.add_column(
        "sessions",
        sa.Column(
            "revalidate_after",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        schema="kernel",
    )
    op.add_column(
        "sessions", sa.Column("revoked_reason", sa.Text(), nullable=True), schema="kernel"
    )
    reasons = ", ".join(f"'{r}'" for r in REVOKE_REASONS)
    op.create_check_constraint(
        op.f("ck_sessions_revoked_reason"),
        "sessions",
        f"revoked_reason IN ({reasons})",
        schema="kernel",
    )
    op.create_check_constraint(
        op.f("ck_sessions_revoked_pair"),
        "sessions",
        "(revoked_at IS NULL) = (revoked_reason IS NULL)",
        schema="kernel",
    )
    op.create_index(
        "ix_sessions_user_id_active",
        "sessions",
        ["user_id"],
        unique=False,
        schema="kernel",
        postgresql_where=sa.text("revoked_at IS NULL"),
    )

    op.create_table(
        "login_attempts",
        sa.Column("id", sa.UUID(), nullable=False),
        # SHA-256 of the 256-bit value in the short-lived login cookie, and of the `state`.
        sa.Column("cookie_hash", sa.LargeBinary(), nullable=False),
        sa.Column("state_hash", sa.LargeBinary(), nullable=False),
        sa.Column("nonce", sa.Text(), nullable=False),
        # The PKCE verifier, encrypted (HKDF label "pkce-verifier", the row's id as AAD).
        sa.Column("code_verifier_enc", sa.LargeBinary(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        # The session being stepped up; null for a login.
        sa.Column("session_id", sa.UUID(), nullable=True),
        sa.Column("next_path", sa.Text(), server_default="/", nullable=False),
        sa.Column("prompt_login", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "purpose IN ('login', 'step_up')", name=op.f("ck_login_attempts_purpose")
        ),
        sa.CheckConstraint(
            "(purpose = 'step_up') = (session_id IS NOT NULL)",
            name=op.f("ck_login_attempts_purpose_session"),
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["kernel.sessions.id"],
            name=op.f("fk_login_attempts_session_id_sessions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_login_attempts")),
        sa.UniqueConstraint("cookie_hash", name=op.f("uq_login_attempts_cookie_hash")),
        sa.UniqueConstraint("state_hash", name=op.f("uq_login_attempts_state_hash")),
        schema="kernel",
    )
    op.create_index(
        op.f("ix_login_attempts_session_id"),
        "login_attempts",
        ["session_id"],
        unique=False,
        schema="kernel",
    )
    op.create_index(
        "ix_login_attempts_expires_at",
        "login_attempts",
        ["expires_at"],
        unique=False,
        schema="kernel",
    )
    # The default privileges of the baseline gave the app role DML on this table. A login
    # attempt is only ever inserted, read and deleted (consumed), never changed.
    op.execute(f"REVOKE UPDATE ON kernel.login_attempts FROM {APP_ROLE}")


def downgrade() -> None:
    # Development only: production migrations are forward-only (ADR-005 §2).
    op.drop_index("ix_login_attempts_expires_at", table_name="login_attempts", schema="kernel")
    op.drop_index(
        op.f("ix_login_attempts_session_id"), table_name="login_attempts", schema="kernel"
    )
    op.drop_table("login_attempts", schema="kernel")
    op.drop_index(
        "ix_sessions_user_id_active",
        table_name="sessions",
        schema="kernel",
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.drop_constraint(op.f("ck_sessions_revoked_pair"), "sessions", schema="kernel")
    op.drop_constraint(op.f("ck_sessions_revoked_reason"), "sessions", schema="kernel")
    op.drop_column("sessions", "revoked_reason", schema="kernel")
    op.drop_column("sessions", "revalidate_after", schema="kernel")
    op.drop_column("sessions", "idp_tokens_enc", schema="kernel")
    op.drop_column("sessions", "csrf_token", schema="kernel")
