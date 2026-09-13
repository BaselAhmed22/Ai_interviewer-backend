"""partial unique index: one active CV/job-description/preference per user

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-12 00:00:00.000000

Same gap as 0004 (one_active_session_per_user), for the three other
"one active record per user" tables: candidate_profiles, job_descriptions,
and interview_preferences each only enforced this in application code
(deactivate_active_records, then insert) — a race between two
near-simultaneous requests for a user with no active row yet can leave
two rows both marked active. This adds the real database-level guarantee.
"""
from typing import Sequence, Union

from alembic import op


revision: str = '0008'
down_revision: Union[str, Sequence[str], None] = '0007'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        'ux_candidate_profiles_one_active_per_user',
        'candidate_profiles',
        ['user_id'],
        unique=True,
        postgresql_where="is_active = true",
    )
    op.create_index(
        'ux_job_descriptions_one_active_per_user',
        'job_descriptions',
        ['user_id'],
        unique=True,
        postgresql_where="is_active = true",
    )
    op.create_index(
        'ux_interview_preferences_one_active_per_user',
        'interview_preferences',
        ['user_id'],
        unique=True,
        postgresql_where="is_active = true",
    )


def downgrade() -> None:
    op.drop_index('ux_interview_preferences_one_active_per_user', table_name='interview_preferences')
    op.drop_index('ux_job_descriptions_one_active_per_user', table_name='job_descriptions')
    op.drop_index('ux_candidate_profiles_one_active_per_user', table_name='candidate_profiles')
