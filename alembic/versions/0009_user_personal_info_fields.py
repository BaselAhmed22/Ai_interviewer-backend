"""add personal info fields to users

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-12 00:00:00.000000

Adds first_name, last_name, phone_number, university, faculty,
target_company, is_graduate, and graduation_year to users. All nullable —
existing rows and the current registration flow (email/password only)
stay valid with no backfill needed.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0009'
down_revision: Union[str, Sequence[str], None] = '0008'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('first_name', sa.String(length=100), nullable=True))
    op.add_column('users', sa.Column('last_name', sa.String(length=100), nullable=True))
    op.add_column('users', sa.Column('phone_number', sa.String(length=20), nullable=True))
    op.add_column('users', sa.Column('university', sa.String(length=255), nullable=True))
    op.add_column('users', sa.Column('faculty', sa.String(length=255), nullable=True))
    op.add_column('users', sa.Column('target_company', sa.String(length=255), nullable=True))
    op.add_column('users', sa.Column('is_graduate', sa.Boolean(), nullable=True))
    op.add_column('users', sa.Column('graduation_year', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'graduation_year')
    op.drop_column('users', 'is_graduate')
    op.drop_column('users', 'target_company')
    op.drop_column('users', 'faculty')
    op.drop_column('users', 'university')
    op.drop_column('users', 'phone_number')
    op.drop_column('users', 'last_name')
    op.drop_column('users', 'first_name')
