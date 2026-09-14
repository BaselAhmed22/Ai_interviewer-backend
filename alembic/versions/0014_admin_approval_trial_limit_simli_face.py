"""admin approval, interview trial limit, simli face id

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-14 02:00:00.000000

- users.is_approved: every account (existing and new) starts pending;
  an admin must approve it before it can start interviews.
- users.interview_attempts_used: counts successful /interviews/prepare
  calls against the free-tier cap.
- interview_sessions.simli_face_id: the candidate's chosen/custom Simli
  avatar face for that interview.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0014'
down_revision: Union[str, Sequence[str], None] = '0013'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column('is_approved', sa.Boolean(), nullable=False, server_default=sa.text('false')),
    )
    op.add_column(
        'users',
        sa.Column('interview_attempts_used', sa.Integer(), nullable=False, server_default='0'),
    )
    op.add_column(
        'interview_sessions',
        sa.Column('simli_face_id', sa.String(length=128), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('interview_sessions', 'simli_face_id')
    op.drop_column('users', 'interview_attempts_used')
    op.drop_column('users', 'is_approved')
