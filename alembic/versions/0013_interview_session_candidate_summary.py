"""add candidate_summary to interview_sessions

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-14 01:00:00.000000

Persists DocumentAgent's CV-vs-job analysis (headline, matched/missing
skills, profile summary) onto the session row, alongside the `questions`
column added in 0012 — both survive past the Redis pipeline context's
TTL and are readable from the history detail endpoint indefinitely.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '0013'
down_revision: Union[str, Sequence[str], None] = '0012'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('interview_sessions', sa.Column('candidate_summary', postgresql.JSON(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    op.drop_column('interview_sessions', 'candidate_summary')
