"""interview_sessions: snapshot job_title/company_name at start time

Revision ID: 0019
Revises: 0018
Create Date: 2026-09-15 09:00:00.000000

InterviewSession already snapshots `questions`/`candidate_summary` at
/interviews/start for exactly this reason: a candidate's active
JobDescription/InterviewPreference can change after an interview
happens, and a "past interviews" list must show what that interview was
actually about, not whatever is currently active. job_title and
company_name join that same snapshot for GET /sessions' dashboard-card
fields (job_position/companyName) — see
app.interviews.services.session_service.create_active_session.

Nullable: historical rows created before this migration have no
snapshot to backfill from.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0019'
down_revision: Union[str, Sequence[str], None] = '0018'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('interview_sessions', sa.Column('job_title', sa.String(length=255), nullable=True))
    op.add_column('interview_sessions', sa.Column('company_name', sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column('interview_sessions', 'company_name')
    op.drop_column('interview_sessions', 'job_title')
