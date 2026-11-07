"""Buyers' update days, from the Weekly Campaign Update Schedule sheet.

Revision ID: 0023_update_schedule
Revises: 0022_sa_alerts
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0023_update_schedule"
down_revision = "0022_sa_alerts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "schedule_entries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("market", sa.String(200), nullable=False),
        sa.Column("weekday", sa.Integer(), nullable=False),
        sa.Column("buyer", sa.String(120), nullable=False),
        sa.Column("email", sa.String(200)),
    )
    op.create_index("ix_schedule_entries_weekday", "schedule_entries", ["weekday"])
    op.create_index("ix_schedule_entries_buyer", "schedule_entries", ["buyer"])


def downgrade() -> None:
    op.drop_table("schedule_entries")
