"""Rate card columns: which CPM is in use, and whether the line is restricted.

Revision ID: 0002_rate_card
Revises: 0001_baseline
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_rate_card"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("line_items", sa.Column("goal_cpm_source", sa.String(20), nullable=True))
    op.add_column(
        "line_items",
        sa.Column("restricted", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("daily_delivery", sa.Column("restricted", sa.String(10), nullable=True))


def downgrade() -> None:
    op.drop_column("daily_delivery", "restricted")
    op.drop_column("line_items", "restricted")
    op.drop_column("line_items", "goal_cpm_source")
