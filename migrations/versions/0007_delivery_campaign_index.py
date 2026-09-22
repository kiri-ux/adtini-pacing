"""Index delivery by the pair a hand-made link identifies it by.

`uq_delivery_grain` leads with the date, so it cannot answer "which rows
belong to this campaign". Every linked line item on an order page was a scan.

Revision ID: 0007_delivery_campaign_index
Revises: 0006_clear_nan_terms
"""
from __future__ import annotations

from alembic import op

revision = "0007_delivery_campaign_index"
down_revision = "0006_clear_nan_terms"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_delivery_campaign",
        "daily_delivery",
        ["data_source", "campaign_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_delivery_campaign", table_name="daily_delivery")
