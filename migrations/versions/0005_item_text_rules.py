"""item text rules: the owner's text edits, kept for every rewrite

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-28 13:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0005'
down_revision: str | None = '0004'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('items', sa.Column('text_rules', postgresql.JSONB(astext_type=sa.Text()), nullable=False,
                                     server_default='[]'))


def downgrade() -> None:
    op.drop_column('items', 'text_rules')
