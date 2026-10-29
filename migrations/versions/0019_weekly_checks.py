"""Weekly checks, one row per check.

The Updates sheet is overwritten every week, which loses the trend buyers
are asked to watch. Each check is kept instead.

Revision ID: 0019_weekly_checks
Revises: 0018_stale_row_check
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0019_weekly_checks"
down_revision = "0018_stale_row_check"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "weekly_checks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "order_id", sa.Integer(),
            sa.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "line_item_id", sa.Integer(),
            sa.ForeignKey("line_items.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("checked_on", sa.Date(), nullable=False),
        sa.Column("data_through", sa.Date(), nullable=True),
        sa.Column("author", sa.String(120), nullable=True),
        sa.Column("ctr", sa.Float(), nullable=True),
        sa.Column("to_date", sa.Float(), nullable=True),
        sa.Column("on_pace", sa.Float(), nullable=True),
        sa.Column("pacing_pct", sa.Float(), nullable=True),
        sa.Column("month_to_date", sa.Float(), nullable=True),
        sa.Column("month_on_pace", sa.Float(), nullable=True),
        sa.Column("month_pacing_pct", sa.Float(), nullable=True),
        sa.Column("conversions", sa.Float(), nullable=True),
        sa.Column("tracking_conversions", sa.Boolean(), nullable=True),
        sa.Column("search_terms_checked", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("optimizations", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(),
                  nullable=True),
    )
    op.create_index("ix_weekly_checks_order_id", "weekly_checks", ["order_id"])
    op.create_index(
        "ix_weekly_checks_line_item", "weekly_checks", ["line_item_id", "checked_on"]
    )


def downgrade() -> None:
    op.drop_index("ix_weekly_checks_line_item", table_name="weekly_checks")
    op.drop_index("ix_weekly_checks_order_id", table_name="weekly_checks")
    op.drop_table("weekly_checks")
