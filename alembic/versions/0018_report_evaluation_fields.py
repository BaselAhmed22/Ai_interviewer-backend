"""interview_reports: real Gemini evaluation fields

Revision ID: 0018
Revises: 0017
Create Date: 2026-09-15 07:00:00.000000

Adds the transcript-based evaluation columns EvaluationAgent now produces
(technical_score, problem_solving_score, communication_score, strengths,
weaknesses, recommendation, summary) alongside the existing placeholder
fields — see app.interviews.models.report.InterviewReport. The old
columns (eye_contact_score, posture_score, speech_clarity_score,
feedback_summary, detailed_metrics) are left in place, nullable, so
historical rows created by the old fixed-sample placeholder evaluator
still load; generate_final_interview_report just stops writing them.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '0018'
down_revision: Union[str, Sequence[str], None] = '0017'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('interview_reports', sa.Column('technical_score', sa.Float(), nullable=True))
    op.add_column('interview_reports', sa.Column('problem_solving_score', sa.Float(), nullable=True))
    op.add_column('interview_reports', sa.Column('communication_score', sa.Float(), nullable=True))
    op.add_column(
        'interview_reports',
        sa.Column('strengths', postgresql.JSON(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        'interview_reports',
        sa.Column('weaknesses', postgresql.JSON(astext_type=sa.Text()), nullable=True),
    )
    op.add_column('interview_reports', sa.Column('recommendation', sa.String(), nullable=True))
    op.add_column('interview_reports', sa.Column('summary', sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column('interview_reports', 'summary')
    op.drop_column('interview_reports', 'recommendation')
    op.drop_column('interview_reports', 'weaknesses')
    op.drop_column('interview_reports', 'strengths')
    op.drop_column('interview_reports', 'communication_score')
    op.drop_column('interview_reports', 'problem_solving_score')
    op.drop_column('interview_reports', 'technical_score')
