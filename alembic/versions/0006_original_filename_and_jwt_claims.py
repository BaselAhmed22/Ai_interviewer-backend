"""add candidate_profiles.original_filename

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-10 00:00:00.000000

Lets CV downloads hand the candidate back the filename they actually
uploaded (e.g. "Ahmed_CV.pdf") instead of the randomized on-disk name
used to avoid collisions.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0006'
down_revision: Union[str, Sequence[str], None] = '0005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('candidate_profiles', sa.Column('original_filename', sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column('candidate_profiles', 'original_filename')
