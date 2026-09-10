"""cv failure tracking + cascading deletes on foreign keys

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-09 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0002'
down_revision: Union[str, Sequence[str], None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'candidate_profiles',
        sa.Column('processing_failed', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column('candidate_profiles', sa.Column('failure_reason', sa.Text(), nullable=True))
    op.alter_column('candidate_profiles', 'processing_failed', server_default=None)

    # Re-create every FK with ON DELETE CASCADE. None of these had an
    # explicit ondelete policy before, so Postgres defaulted to NO ACTION —
    # deleting a user would previously fail outright with a foreign key
    # violation instead of cleaning up their dependent rows.
    op.drop_constraint('candidate_profiles_user_id_fkey', 'candidate_profiles', type_='foreignkey')
    op.create_foreign_key(
        'candidate_profiles_user_id_fkey', 'candidate_profiles', 'users', ['user_id'], ['id'], ondelete='CASCADE'
    )

    op.drop_constraint('job_descriptions_user_id_fkey', 'job_descriptions', type_='foreignkey')
    op.create_foreign_key(
        'job_descriptions_user_id_fkey', 'job_descriptions', 'users', ['user_id'], ['id'], ondelete='CASCADE'
    )

    op.drop_constraint('interview_preferences_user_id_fkey', 'interview_preferences', type_='foreignkey')
    op.create_foreign_key(
        'interview_preferences_user_id_fkey',
        'interview_preferences',
        'users',
        ['user_id'],
        ['id'],
        ondelete='CASCADE',
    )

    op.drop_constraint('interview_sessions_user_id_fkey', 'interview_sessions', type_='foreignkey')
    op.create_foreign_key(
        'interview_sessions_user_id_fkey', 'interview_sessions', 'users', ['user_id'], ['id'], ondelete='CASCADE'
    )

    op.drop_constraint('interview_reports_session_id_fkey', 'interview_reports', type_='foreignkey')
    op.create_foreign_key(
        'interview_reports_session_id_fkey',
        'interview_reports',
        'interview_sessions',
        ['session_id'],
        ['id'],
        ondelete='CASCADE',
    )


def downgrade() -> None:
    op.drop_constraint('interview_reports_session_id_fkey', 'interview_reports', type_='foreignkey')
    op.create_foreign_key(
        'interview_reports_session_id_fkey', 'interview_reports', 'interview_sessions', ['session_id'], ['id']
    )

    op.drop_constraint('interview_sessions_user_id_fkey', 'interview_sessions', type_='foreignkey')
    op.create_foreign_key('interview_sessions_user_id_fkey', 'interview_sessions', 'users', ['user_id'], ['id'])

    op.drop_constraint('interview_preferences_user_id_fkey', 'interview_preferences', type_='foreignkey')
    op.create_foreign_key(
        'interview_preferences_user_id_fkey', 'interview_preferences', 'users', ['user_id'], ['id']
    )

    op.drop_constraint('job_descriptions_user_id_fkey', 'job_descriptions', type_='foreignkey')
    op.create_foreign_key('job_descriptions_user_id_fkey', 'job_descriptions', 'users', ['user_id'], ['id'])

    op.drop_constraint('candidate_profiles_user_id_fkey', 'candidate_profiles', type_='foreignkey')
    op.create_foreign_key('candidate_profiles_user_id_fkey', 'candidate_profiles', 'users', ['user_id'], ['id'])

    op.drop_column('candidate_profiles', 'failure_reason')
    op.drop_column('candidate_profiles', 'processing_failed')
