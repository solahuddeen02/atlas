"""add_installed_plugins

Revision ID: f50a6772da01
Revises: b2c3d4e5f6a7
Create Date: 2026-05-07 17:16:21.121304

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f50a6772da01'
down_revision: Union[str, Sequence[str], None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("installed_plugins"):
        op.create_table(
            "installed_plugins",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("name", sa.Text(), nullable=False),
            sa.Column("version", sa.Text(), nullable=False),
            sa.Column("package", sa.Text(), nullable=True),
            sa.Column("installed_at", sa.Text(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("name"),
        )


def downgrade() -> None:
    op.drop_table("installed_plugins")
