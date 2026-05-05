"""add tenant model

Revision ID: d4e8f2a9b3c5
Revises: c1fdc6c1bce2
Create Date: 2026-05-05 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd4e8f2a9b3c5'
down_revision: Union[str, Sequence[str], None] = 'c1fdc6c1bce2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'tenants',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('name', sa.Text(), nullable=False),
        sa.Column('slug', sa.Text(), nullable=False),
        sa.Column('created_at', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('slug'),
    )
    op.create_table(
        'tenant_members',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('tenant_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('role', sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id']),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.add_column('objects', sa.Column('tenant_id', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('objects', 'tenant_id')
    op.drop_table('tenant_members')
    op.drop_table('tenants')
