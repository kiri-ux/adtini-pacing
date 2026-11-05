"""StackAdapt pacing alerts posted by the launcher's nightly run.

Revision ID: 0022_sa_alerts
Revises: 0021_strategy_targets
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0022_sa_alerts"
down_revision = "0021_strategy_targets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sa_alerts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_date", sa.Date(), nullable=False),
        sa.Column("external_order_id", sa.String(64)),
        sa.Column("external_line_item_id", sa.String(64)),
        sa.Column("group_id", sa.String(64)),
        sa.Column("group_name", sa.String(400)),
        sa.Column("campaign_id", sa.String(64)),
        sa.Column("campaign_name", sa.String(400)),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("alert", sa.Text()),
        sa.Column("adjustment", sa.Text()),
        sa.Column("status", sa.String(300)),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index("ix_sa_alerts_run_date", "sa_alerts", ["run_date"])
    op.create_index("ix_sa_alerts_line_item", "sa_alerts", ["external_line_item_id", "run_date"])


def downgrade() -> None:
    op.drop_table("sa_alerts")
