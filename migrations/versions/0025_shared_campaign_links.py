"""A campaign can be linked to several line items, split by their dates.

Revision ID: 0025_shared_campaign_links
Revises: 0024_strategy_links
"""
from __future__ import annotations

from alembic import op

revision = "0025_shared_campaign_links"
down_revision = "0024_strategy_links"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("campaign_links") as batch:
        batch.drop_constraint("uq_campaign_link", type_="unique")
        batch.create_unique_constraint(
            "uq_campaign_link", ["data_source", "campaign_id", "line_item_id"]
        )


def downgrade() -> None:
    with op.batch_alter_table("campaign_links") as batch:
        batch.drop_constraint("uq_campaign_link", type_="unique")
        batch.create_unique_constraint("uq_campaign_link", ["data_source", "campaign_id"])
