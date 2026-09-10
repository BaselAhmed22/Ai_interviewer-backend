"""partial unique index: one IN_PROGRESS session per user

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-10 00:00:00.000000

start_session only guarded against a duplicate active session with a
SELECT-then-INSERT check in application code, which two near-simultaneous
requests can both pass before either commits. This adds the real
database-level guarantee; the app now catches the resulting
IntegrityError and returns the same 409 it already used for the
app-level check.
"""
from typing import Sequence, Union

from alembic import op


revision: str = '0004'
down_revision: Union[str, Sequence[str], None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        'ux_interview_sessions_one_active_per_user',
        'interview_sessions',
        ['user_id'],
        unique=True,
        postgresql_where="status = 'IN_PROGRESS'",
    )


def downgrade() -> None:
    op.drop_index('ux_interview_sessions_one_active_per_user', table_name='interview_sessions')
