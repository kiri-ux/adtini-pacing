"""Hand-made links between a DSP campaign and the line item it was bought on.

Revision ID: 0005_campaign_links
Revises: 0004_strategy_terms
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_campaign_links"
down_revision = "0004_strategy_terms"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "campaign_links",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "line_item_id",
            sa.Integer(),
            sa.ForeignKey("line_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("data_source", sa.String(120), nullable=False),
        sa.Column("campaign_id", sa.String(64), nullable=False),
        sa.Column("campaign_name", sa.String(400), nullable=True),
        sa.Column(
            "ops_verified", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("linked_by", sa.String(120), nullable=True),
        sa.Column(
            "linked_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("data_source", "campaign_id", name="uq_campaign_link"),
    )
    op.create_index("ix_campaign_links_line_item_id", "campaign_links", ["line_item_id"])
    op.create_index("ix_campaign_links_campaign_id", "campaign_links", ["campaign_id"])


def downgrade() -> None:
    op.drop_table("campaign_links")
