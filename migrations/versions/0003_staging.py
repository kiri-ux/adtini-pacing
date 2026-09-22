"""Staging table for chunked delivery loads.

Revision ID: 0003_staging
Revises: 0002_rate_card
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_staging"
down_revision = "0002_rate_card"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "delivery_staging",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("data_source", sa.String(120), nullable=False),
        sa.Column("campaign_id", sa.String(64), nullable=False),
        sa.Column("strategy_id", sa.String(64), nullable=False),
        sa.Column("business_unit", sa.String(200), nullable=True),
        sa.Column("client_name", sa.String(300), nullable=True),
        sa.Column("external_order_id", sa.String(64), nullable=True),
        sa.Column("external_line_item_id", sa.String(64), nullable=True),
        sa.Column("order_level_name", sa.String(400), nullable=True),
        sa.Column("line_item_name", sa.String(400), nullable=True),
        sa.Column("strategy_name", sa.String(400), nullable=True),
        sa.Column("strategy_type", sa.String(120), nullable=True),
        sa.Column("product", sa.String(120), nullable=True),
        sa.Column("restricted", sa.String(10), nullable=True),
        sa.Column("campaign_name", sa.String(400), nullable=True),
        sa.Column("campaign_start_date", sa.Date(), nullable=True),
        sa.Column("impressions", sa.Float(), nullable=False),
        sa.Column("clicks", sa.Float(), nullable=False),
        sa.Column("cost", sa.Float(), nullable=False),
        sa.Column("conversions", sa.Float(), nullable=False),
        sa.Column("viewthroughs", sa.Float(), nullable=False),
        sa.Column("click_conversions", sa.Float(), nullable=False),
        sa.Column("goal_cpm", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("delivery_staging")
