"""Saves shared across workers, so each drops its own cached pages.

Revision ID: 0031_cache_edits
Revises: 0030_unpin_adopted_orders
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0031_cache_edits"
down_revision = "0030_unpin_adopted_orders"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cache_edits",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("order_id", sa.Integer(), nullable=True),
        sa.Column("at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_cache_edits_at", "cache_edits", ["at"])


def downgrade() -> None:
    op.drop_index("ix_cache_edits_at", table_name="cache_edits")
    op.drop_table("cache_edits")
