"""Delivery a buyer linked to a strategy row by hand.

Revision ID: 0024_strategy_links
Revises: 0023_update_schedule
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0024_strategy_links"
down_revision = "0023_update_schedule"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("strategy_terms", sa.Column("linked_keys", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("strategy_terms", "linked_keys")
