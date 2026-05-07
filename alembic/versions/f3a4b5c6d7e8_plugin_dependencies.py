"""plugin_dependencies

Revision ID: f3a4b5c6d7e8
Revises: e1f2a3b4c5d6
Create Date: 2026-05-07 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'f3a4b5c6d7e8'
down_revision: Union[str, Sequence[str], None] = 'e1f2a3b4c5d6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    cols = {c["name"] for c in sa.inspect(bind).get_columns("installed_plugins")}
    if "dependencies" not in cols:
        op.add_column(
            "installed_plugins",
            sa.Column("dependencies", sa.Text(), nullable=False, server_default="[]"),
        )


def downgrade() -> None:
    with op.batch_alter_table("installed_plugins") as batch_op:
        batch_op.drop_column("dependencies")
