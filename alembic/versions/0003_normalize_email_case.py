"""normalize existing user emails to lowercase

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-10 00:00:00.000000

The API now lowercases incoming emails before every lookup (register,
login, forgot-password). Existing rows created before that change may
still hold mixed-case values, which would silently stop matching those
lookups. This backfills them once; verified against the live DB that no
two existing rows collide only by casing before running.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0003'
down_revision: Union[str, Sequence[str], None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(sa.text("UPDATE users SET email = lower(email) WHERE email <> lower(email)"))


def downgrade() -> None:
    # Casing is not recoverable once lowercased — this migration is not
    # reversible in the strict sense, so downgrade is a no-op.
    pass
