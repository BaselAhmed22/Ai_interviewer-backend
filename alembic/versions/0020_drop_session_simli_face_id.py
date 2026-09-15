"""interview_sessions: drop simli_face_id

Revision ID: 0020
Revises: 0019
Create Date: 2026-09-15 15:00:00.000000

Per-request custom Simli avatar face selection is removed — the LiveKit
worker now always falls back to the SIMLI_FACE_ID env default (see
app.interviews.workers.livekit_agent). candidate_profiles,
job_descriptions, and interview_preferences tables are intentionally
NOT touched by this cleanup pass: the standalone /candidates/* routes
that wrote to them were removed, but the tables stay in place (unused
by application code going forward) rather than being dropped, since
they may still hold data worth keeping and dropping them is not
reversible.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0020'
down_revision: Union[str, Sequence[str], None] = '0019'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column('interview_sessions', 'simli_face_id')


def downgrade() -> None:
    op.add_column('interview_sessions', sa.Column('simli_face_id', sa.String(length=128), nullable=True))
