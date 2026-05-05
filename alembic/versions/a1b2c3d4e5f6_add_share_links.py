"""add share_links table

Revision ID: a1b2c3d4e5f6
Revises: d4e8f2a9b3c5
Create Date: 2026-05-05 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "a1b2c3d4e5f6"
down_revision = "d4e8f2a9b3c5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("share_links"):
        op.create_table(
            "share_links",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("token", sa.Text, nullable=False, unique=True),
            sa.Column("object_id", sa.Integer, sa.ForeignKey("objects.id"), nullable=False),
            sa.Column("tenant_id", sa.Integer, sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column("created_by", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
            sa.Column("expires_at", sa.Text, nullable=True),
            sa.Column("created_at", sa.Text, nullable=False),
        )
        op.create_index("ix_share_links_token", "share_links", ["token"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_share_links_token", "share_links")
    op.drop_table("share_links")
