"""price research warnings: which price sources failed (shown on the card)

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-28 16:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0007'
down_revision: str | None = '0006'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('price_research', sa.Column('warnings', postgresql.JSONB(astext_type=sa.Text()), nullable=False,
                                              server_default='[]'))


def downgrade() -> None:
    op.drop_column('price_research', 'warnings')
