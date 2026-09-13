"""target_company as enum (+ other), phone_country_code

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-12 00:00:00.000000

target_company moves from a free-text string to a fixed enum (Google,
Microsoft, Amazon, Meta, Apple, Startup, Other), with target_company_other
holding the free-text name when Other is picked. phone_country_code is
new — phone_number now holds the national number only, validated against
its country code's expected length in app.schemas.auth.

Existing free-text target_company values (if any) don't map cleanly onto
the new enum, so they're cleared rather than guessed at.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '0010'
down_revision: Union[str, Sequence[str], None] = '0009'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ENUM_VALUES = ('Google', 'Microsoft', 'Amazon', 'Meta', 'Apple', 'Startup', 'Other')


def upgrade() -> None:
    op.execute('UPDATE users SET target_company = NULL')

    postgresql.ENUM(*_ENUM_VALUES, name='targetcompany').create(op.get_bind(), checkfirst=True)
    target_company_enum = postgresql.ENUM(*_ENUM_VALUES, name='targetcompany', create_type=False)

    op.alter_column(
        'users', 'target_company',
        type_=target_company_enum,
        postgresql_using='NULL',
    )
    op.add_column('users', sa.Column('target_company_other', sa.String(length=255), nullable=True))
    op.add_column('users', sa.Column('phone_country_code', sa.String(length=5), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'phone_country_code')
    op.drop_column('users', 'target_company_other')
    op.alter_column('users', 'target_company', type_=sa.String(length=255), postgresql_using='target_company::text')
    postgresql.ENUM(name='targetcompany').drop(op.get_bind(), checkfirst=True)
