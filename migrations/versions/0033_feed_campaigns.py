"""Campaign names the feed has used, kept up by the load.

Revision ID: 0033_feed_campaigns
Revises: 0032_render_timings
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0033_feed_campaigns"
down_revision = "0032_render_timings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "feed_campaigns",
        sa.Column("data_source", sa.String(120), primary_key=True),
        sa.Column("campaign_id", sa.String(64), primary_key=True),
        sa.Column("campaign_name", sa.String(400), primary_key=True),
        sa.Column("last_date", sa.Date(), nullable=False),
    )
    op.create_index("ix_feed_campaigns_last_date", "feed_campaigns", ["last_date"])
    # Filled once from the last 150 days, in the database.
    op.execute(
        """
        INSERT INTO feed_campaigns (data_source, campaign_id, campaign_name, last_date)
        SELECT data_source, campaign_id, campaign_name, MAX(date)
        FROM daily_delivery
        WHERE campaign_name IS NOT NULL AND data_source IS NOT NULL
          AND campaign_id IS NOT NULL
          AND date >= (SELECT MAX(date) FROM daily_delivery) - 150
        GROUP BY data_source, campaign_id, campaign_name
        """
        if op.get_bind().dialect.name == "postgresql" else
        """
        INSERT INTO feed_campaigns (data_source, campaign_id, campaign_name, last_date)
        SELECT data_source, campaign_id, campaign_name, MAX(date)
        FROM daily_delivery
        WHERE campaign_name IS NOT NULL AND data_source IS NOT NULL
          AND campaign_id IS NOT NULL
          AND date >= date((SELECT MAX(date) FROM daily_delivery), '-150 days')
        GROUP BY data_source, campaign_id, campaign_name
        """
    )


def downgrade() -> None:
    op.drop_index("ix_feed_campaigns_last_date", table_name="feed_campaigns")
    op.drop_table("feed_campaigns")
