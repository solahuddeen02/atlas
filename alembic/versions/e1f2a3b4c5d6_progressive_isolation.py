"""progressive_isolation

Revision ID: e1f2a3b4c5d6
Revises: f50a6772da01
Create Date: 2026-05-07 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e1f2a3b4c5d6'
down_revision: Union[str, Sequence[str], None] = 'f50a6772da01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    installed_cols = {c["name"] for c in inspector.get_columns("installed_plugins")}
    if "runtime" not in installed_cols:
        op.add_column(
            "installed_plugins",
            sa.Column("runtime", sa.Text(), nullable=False, server_default="inprocess"),
        )
    if "port" not in installed_cols:
        op.add_column(
            "installed_plugins",
            sa.Column("port", sa.Integer(), nullable=True),
        )

    tenant_cols = {c["name"] for c in inspector.get_columns("tenants")}
    if "db_url" not in tenant_cols:
        op.add_column("tenants", sa.Column("db_url", sa.Text(), nullable=True))
    if "storage_path" not in tenant_cols:
        op.add_column("tenants", sa.Column("storage_path", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("installed_plugins") as batch_op:
        batch_op.drop_column("port")
        batch_op.drop_column("runtime")

    with op.batch_alter_table("tenants") as batch_op:
        batch_op.drop_column("storage_path")
        batch_op.drop_column("db_url")
