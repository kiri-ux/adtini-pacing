"""A running commentary beside the delivery, per day.

The hand-kept sheets carry one - "lowered budget", "creative swapped",
"client paused for the holiday" - and without it a dip in the daily grid is
unexplainable a month later.

Revision ID: 0013_day_notes
Revises: 0012_sold_strategies
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013_day_notes"
down_revision = "0012_sold_strategies"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "day_notes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "order_id",
            sa.Integer(),
            sa.ForeignKey("orders.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("author", sa.String(120), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_day_notes_order_id", "day_notes", ["order_id"])
    op.create_index("ix_day_notes_order_date", "day_notes", ["order_id", "date"])


def downgrade() -> None:
    op.drop_table("day_notes")
