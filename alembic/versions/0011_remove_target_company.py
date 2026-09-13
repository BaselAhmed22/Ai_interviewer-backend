"""remove target_company

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-13 00:00:00.000000

target_company / target_company_other are dropped entirely — the
"predefined company list + Other" requirement was replaced by a
predefined phone-country-code dropdown instead (see app.schemas.auth).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '0011'
down_revision: Union[str, Sequence[str], None] = '0010'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ENUM_VALUES = ('Google', 'Microsoft', 'Amazon', 'Meta', 'Apple', 'Startup', 'Other')


def upgrade() -> None:
    op.drop_column('users', 'target_company_other')
    op.drop_column('users', 'target_company')
    postgresql.ENUM(name='targetcompany').drop(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    postgresql.ENUM(*_ENUM_VALUES, name='targetcompany').create(op.get_bind(), checkfirst=True)
    target_company_enum = postgresql.ENUM(*_ENUM_VALUES, name='targetcompany', create_type=False)
    op.add_column('users', sa.Column('target_company', target_company_enum, nullable=True))
    op.add_column('users', sa.Column('target_company_other', sa.String(length=255), nullable=True))
