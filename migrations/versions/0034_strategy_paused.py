"""A buyer can flag a strategy row as paused (it shouldn't be serving).

Revision ID: 0034_strategy_paused
Revises: 0033_feed_campaigns
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0034_strategy_paused"
down_revision = "0033_feed_campaigns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("strategy_terms", sa.Column("paused", sa.Boolean(), nullable=False,
                                              server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("strategy_terms", "paused")
