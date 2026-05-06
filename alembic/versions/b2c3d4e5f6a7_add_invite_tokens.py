"""add invite_tokens table

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-05-06 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "b2c3d4e5f6a7"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("invite_tokens"):
        op.create_table(
            "invite_tokens",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("token", sa.Text, nullable=False, unique=True),
            sa.Column("tenant_id", sa.Integer, sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column("created_by", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
            sa.Column("role", sa.Text, nullable=False, default="member"),
            sa.Column("expires_at", sa.Text, nullable=True),
            sa.Column("used_at", sa.Text, nullable=True),
            sa.Column("created_at", sa.Text, nullable=False),
        )


def downgrade() -> None:
    op.drop_table("invite_tokens")
