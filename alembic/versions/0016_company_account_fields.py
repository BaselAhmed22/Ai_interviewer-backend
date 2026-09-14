"""dual registration: individual vs company account fields

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-14 20:00:00.000000

Adds account_type (individual "user" vs "company", defaulting existing
rows to "user") plus company-only fields: company_name, country,
position, specialization, company_size, website, is_company_admin.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0016'
down_revision: Union[str, Sequence[str], None] = '0015'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ACCOUNT_TYPE_ENUM = sa.Enum('user', 'company', name='accounttype')


def upgrade() -> None:
    _ACCOUNT_TYPE_ENUM.create(op.get_bind(), checkfirst=True)
    op.add_column(
        'users',
        sa.Column('account_type', _ACCOUNT_TYPE_ENUM, nullable=False, server_default='user'),
    )
    op.add_column('users', sa.Column('company_name', sa.String(length=255), nullable=True))
    op.add_column('users', sa.Column('country', sa.String(length=100), nullable=True))
    op.add_column('users', sa.Column('position', sa.String(length=150), nullable=True))
    op.add_column('users', sa.Column('specialization', sa.String(length=255), nullable=True))
    op.add_column('users', sa.Column('company_size', sa.String(length=20), nullable=True))
    op.add_column('users', sa.Column('website', sa.String(length=255), nullable=True))
    op.add_column(
        'users',
        sa.Column('is_company_admin', sa.Boolean(), nullable=False, server_default=sa.text('false')),
    )


def downgrade() -> None:
    op.drop_column('users', 'is_company_admin')
    op.drop_column('users', 'website')
    op.drop_column('users', 'company_size')
    op.drop_column('users', 'specialization')
    op.drop_column('users', 'position')
    op.drop_column('users', 'country')
    op.drop_column('users', 'company_name')
    op.drop_column('users', 'account_type')
    _ACCOUNT_TYPE_ENUM.drop(op.get_bind(), checkfirst=True)
