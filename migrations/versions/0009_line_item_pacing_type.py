"""A line item can pace differently from the rest of its order.

An order carrying Display alongside Pay-Per-Click has one line sold in
impressions and another in ad spend. Pacing both the way the order paces
answers neither. Null means the line paces however the order does, which is
every row that exists today.

Revision ID: 0009_line_item_pacing_type
Revises: 0008_repair_derived_terms
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_line_item_pacing_type"
down_revision = "0008_repair_derived_terms"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "line_items", sa.Column("pacing_type", sa.String(20), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("line_items", "pacing_type")
