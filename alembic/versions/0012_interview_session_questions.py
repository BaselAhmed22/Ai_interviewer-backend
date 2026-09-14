"""add questions to interview_sessions

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-14 00:00:00.000000

Persists the prepared question list (currently only held ephemerally in
Redis via AgentContext, keyed by preparation_id) onto the session row
itself, so an interview's questions are still readable from the history
detail endpoint long after that Redis key has expired. Only ever
populated by the multi-agent pipeline's /interviews/start — the plain
/sessions/start path has no prepared questions and leaves this null.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '0012'
down_revision: Union[str, Sequence[str], None] = '0011'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('interview_sessions', sa.Column('questions', postgresql.JSON(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    op.drop_column('interview_sessions', 'questions')
