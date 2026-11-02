"""price research shop prices: new-unit prices in Cyprus shops, shown next to the official price

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-02 12:50:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0008'
down_revision: str | None = '0007'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('price_research', sa.Column('shop_prices', postgresql.JSONB(astext_type=sa.Text()), nullable=False,
                                              server_default='[]'))


def downgrade() -> None:
    op.drop_column('price_research', 'shop_prices')
