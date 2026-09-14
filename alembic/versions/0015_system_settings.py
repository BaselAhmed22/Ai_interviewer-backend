"""system_settings table (require_admin_approval feature flag)

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-14 03:00:00.000000

A single-row runtime settings table, edited via PATCH
/api/v1/admin/settings, so an admin can toggle require_admin_approval
without a redeploy. Seeded with the one row (id=1) the app always reads/
writes — see app.services.system_settings_service.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0015'
down_revision: Union[str, Sequence[str], None] = '0014'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'system_settings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('require_admin_approval', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.execute("INSERT INTO system_settings (id, require_admin_approval) VALUES (1, false)")


def downgrade() -> None:
    op.drop_table('system_settings')
