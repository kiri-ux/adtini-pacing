"""The strategies the client bought, from the orders export.

The export carries a strategy column per product - "Meta Strategy", "Display
Strategy", "OTT + Video Strategy" - and none of it was being read. Those
columns sat in the unmapped list while the strategy tab inferred everything
from what happened to be running.

Revision ID: 0012_sold_strategies
Revises: 0011_strategy_label_per_product
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012_sold_strategies"
down_revision = "0011_strategy_label_per_product"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "line_items", sa.Column("sold_strategies", sa.Text(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("line_items", "sold_strategies")
