"""publication preview message: the bot message showing a draft version, so a reply to it edits that version

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-28 15:05:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0006'
down_revision: str | None = '0005'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('publications', sa.Column('preview_message_id', sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column('publications', 'preview_message_id')
